import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from typing import Any

import jsonpickle
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from humanfriendly import format_timespan
from tqdm import tqdm

# not sure why our debug logs from other modules only work when using root logger? not good practice but
logger = logging.getLogger()


class GeodeVersionState(str, Enum):
    pending = "pending"
    rejected = "rejected"
    unlisted = "unlisted"
    verified = "verified"


class NetworkError(Exception):
    pass


class NetworkRateLimitedError(Exception):
    pass


class Network404Error(Exception):
    pass


REPO_INFO_GRAPHQL = """
query ($owner: String!, $name: String!, $cursor: String) {
    repository(owner: $owner, name: $name) {
        name
        defaultBranchRef {
            name
            target {
                ... on Commit {
                    oid

                    history(first: 25, after: $cursor) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            committedDate
                            additions
                            deletions
                        }
                    }
                }
            }
        }
    }

    rateLimit {
        cost
        remaining
        resetAt
    }
}
"""


class Utils:
    @staticmethod
    def github_auth_headers():
        return {"Authorization": f"Bearer {os.environ["GITHUB_API_TOKEN"]}"}

    @staticmethod
    def geode_auth_headers():
        return {"Authorization": f"Bearer {os.environ["GEODE_API_TOKEN"]}"}


class NetworkUtils:
    @staticmethod
    def raw(
        full_url: str,
        params_or_json: dict[str, Any] = {},
        headers: dict[str, str] = {},
        method="GET",
    ):
        request = requests.Request(
            method,
            full_url,
            params=params_or_json if method == "GET" else None,
            json=params_or_json if method == "POST" else None,
            headers=headers,
        )
        prepared = request.prepare()

        logger.debug(f"Fetching {prepared.url}")

        session = requests.Session()
        response = session.send(prepared)

        if response.status_code == 404:
            raise Network404Error(f"{request.url} returned 404 response code!")

        if response.status_code == 403 or response.status_code == 429:
            raise NetworkRateLimitedError(
                f"{"GitHub" if response.status_code == 403 else "Geode"} API has been rate-limited!"
            )

        # if response.status_code != 200:
        #     raise NetworkError(f"{request.url} response code was not 200!")

        json = response.json()

        if str(type(json)) == "<class 'list'>":
            json = {"__data": json}

        link_header = response.headers.get("Link")
        if link_header:
            for part in link_header.split(", "):
                url, rel = part.split(";")
                if rel.strip() == 'rel="next"':
                    json["__internal_github_next_url"] = url.strip()[1:-1]
                    break

        if "data" in json and "rateLimit" in json["data"]:
            logger.debug(
                f"GitHub GraphQL remaining: {json["data"]["rateLimit"]["remaining"]} (resets in {format_timespan(datetime.fromisoformat(json["data"]["rateLimit"]["resetAt"]).timestamp() - time.time())})"
            )
        else:
            rate_limit_remaining = response.headers.get("X-Ratelimit-Remaining")
            rate_limit_reset = response.headers.get("X-Ratelimit-Reset")
            if rate_limit_remaining and rate_limit_reset:
                logger.debug(
                    f"GitHub REST remaining: {rate_limit_remaining} (resets in {format_timespan(int(rate_limit_reset) - time.time())})"
                )

        return json

    @staticmethod
    def geode(endpoint: str, data: dict[str, Any] = {}):
        url_base = os.environ["GEODE_API_ENDPOINT"]

        json_data = NetworkUtils.raw(
            f"{url_base}{endpoint}",
            params_or_json=data,
            headers=Utils.geode_auth_headers(),
        )

        if "error" in json_data and len(json_data["error"]) != 0:
            raise NetworkError(f"Error when fetching {endpoint} from Geode: {json_data["error"]}")

        return json_data["payload"]

    @staticmethod
    def geode_paginated(endpoint: str, data: dict[str, Any] = {}, limit: int = -1):
        data["page"] = 1
        data["per_page"] = min(limit, 100) if limit != -1 else 100

        json_data = NetworkUtils.geode(endpoint, data)

        if limit != -1:
            if json_data["count"] > limit:
                json_data["count"] = limit

        while data["page"] * data["per_page"] < json_data["count"]:
            data["page"] += 1
            json_data["data"].extend(NetworkUtils.geode(endpoint, data)["data"])

        return json_data["data"]

    @staticmethod
    def github(endpoint: str, data: dict[str, Any] = {}):
        url_base = os.environ["GITHUB_API_ENDPOINT"]

        full_url = endpoint if "://" in endpoint else f"{url_base}{endpoint}"

        json_data = NetworkUtils.raw(full_url, params_or_json=data, headers=Utils.github_auth_headers())

        if "message" in json_data:
            raise NetworkError(
                f"Error when fetching {endpoint} from GitHub: {json_data["message"]} (see {json_data["documentation_url"]})"
            )

        return json_data

    @staticmethod
    def github_paginated(endpoint: str, required_data: str, data: dict[str, Any] = {}):
        data["page"] = 1
        data["per_page"] = 100
        json_data = NetworkUtils.github(endpoint, data)
        next_url = ""

        if "__internal_github_next_url" in json_data:
            while json_data["__internal_github_next_url"]:
                data["page"] += 1
                next_url = json_data["__internal_github_next_url"]
                next_data = NetworkUtils.github(next_url)
                json_data[required_data].extend(next_data[required_data])
                if "__internal_github_next_url" in next_data:
                    json_data["__internal_github_next_url"] = next_data["__internal_github_next_url"]
                else:
                    json_data["__internal_github_next_url"] = None

        return json_data[required_data]


class DeveloperGithubCommit:
    def __init__(self, json_data):
        self.date: str = json_data["committedDate"]
        self.additions: int = json_data["additions"]
        self.deletions: int = json_data["deletions"]


class DeveloperGithubRepository:
    def __init__(self, json_data):
        full_name = json_data["full_name"]
        name, repo = full_name.split("/")
        actions_data = NetworkUtils.github_paginated(f"/repos/{full_name}/actions/runs", "workflow_runs")
        self.total_action_runs = len([run for run in actions_data if run["status"] == "completed"])
        self.failed_action_runs = len([run for run in actions_data if run["conclusion"] == "failure"])

        latest_commit_sha = None

        self.commits: list[DeveloperGithubCommit] = []

        cursor = None
        while True:
            json = {
                "query": REPO_INFO_GRAPHQL,
                "variables": {"owner": name, "name": repo, "cursor": cursor},
            }

            data = NetworkUtils.raw(
                f"{os.environ["GITHUB_API_ENDPOINT"]}/graphql",
                json,
                Utils.github_auth_headers(),
                "POST",
            )

            if "errors" in data:
                raise NetworkError(f"GraphQL Error!\n{jsonpickle.dumps(data["errors"], indent=4)}")

            repositoryInfo = data["data"]["repository"]

            # we are fetching commit sha through graphql instead of another rest api request#
            # so if we dont have it yet, get it and use it to get the repo tree
            if not latest_commit_sha:
                latest_commit_sha = repositoryInfo["defaultBranchRef"]["target"]["oid"]
                repo_tree = NetworkUtils.github(
                    f"/repos/{full_name}/git/trees/{latest_commit_sha}",
                    {"recursive": 1},
                )

                if repo_tree["truncated"]:
                    # what the fuck
                    # not even globed gets truncated
                    logger.warning(
                        f"Repo {full_name} is too large and so the file count will be incorrect! Manually traversing file trees may be implemented in the future."
                    )

                self.file_count = len([file for file in repo_tree["tree"] if file["type"] == "blob"])

            history = repositoryInfo["defaultBranchRef"]["target"]["history"]

            for commit in history["nodes"]:
                self.commits.append(DeveloperGithubCommit(commit))

            if not history["pageInfo"]["hasNextPage"]:
                break

            cursor = history["pageInfo"]["endCursor"]


class DeveloperGeodeModVersion:
    def __init__(self, json_data):
        self.downloads: int = json_data["download_count"]
        self.state: GeodeVersionState = json_data["status"]
        self.name: str = json_data["name"]
        self.date: str = json_data["updated_at"]


class DeveloperGeodeMod:
    def __init__(self, json_data):
        # NOTE: /versions does NOT work authenticated
        self.versions = [
            DeveloperGeodeModVersion(data) for data in NetworkUtils.geode(f"/mods/{json_data["id"]}")["versions"]
        ]
        self.downloads: int = json_data["download_count"]
        self.featured: bool = json_data["featured"]
        self.developer_count = len(json_data["developers"])

        latest_version_info = NetworkUtils.geode(f"/mods/{json_data["id"]}/versions/latest")
        self.dependency_count = len(latest_version_info["dependencies"])
        self.creation_date = latest_version_info["created_at"]

        if os.environ["FETCH_GITHUB_REPO_DATA"] != "true":
            return

        # regex taken from https://github.com/geode-sdk/website/blob/6b9d67/src/routes/mods/%5Bid%5D/%2Bpage.svelte#L351
        # and slightly modified

        regex = r"^(?:https?:\/\/)?github\.com\/([\w-]+\/[\w-]+)(?:\/|$)"

        source_link: str | None = None

        if "direct_download_link" in latest_version_info or "github" not in latest_version_info["direct_download_link"]:
            # for fancy pants index staff
            source_link = latest_version_info["direct_download_link"]
        elif json_data["links"] != None:
            source_link = json_data["links"]["source"]

        if not source_link:
            self.github_repo = None
            return

        regex_match = re.match(regex, source_link)

        if not regex_match:
            return

        pair = regex_match.group(1)

        try:
            self.github_repo = DeveloperGithubRepository(NetworkUtils.github(f"/repos/{pair}"))
        except Network404Error:
            logger.warning(f"Repository linked ({pair}) does not exist!")


class DeveloperGithubInfo:
    def __init__(self, json_data):
        self.follower_count: int = json_data["followers"]


class Developer:
    def __init__(self, json_data):
        self.admin: bool = json_data["admin"]
        self.verified: bool = json_data["verified"]

        self.display_name: str = json_data["display_name"]
        self.username: str = json_data["username"]
        self.id: int = json_data["id"]

        self.github_id: int = json_data["github_id"]

        self.mods = [
            DeveloperGeodeMod(data) for data in NetworkUtils.geode_paginated("/mods", {"developer": self.username})
        ]

        if len(self.mods) == 0:
            self.github_data = {}
            return

        def try_fetch_gh_data() -> bool:
            try:
                github_data = NetworkUtils.github(f"/user/{self.github_id}")
                self.github_data = DeveloperGithubInfo(github_data)
                return True
            except Network404Error:
                logger.warning(f"GitHub account associated with {self.username} does not exist!")
                return False

        for _ in range(15):
            try:
                if not try_fetch_gh_data():
                    continue

                logger.info("failed to fetch github data, trying again after 3s")
                time.sleep(3)
            except:
                pass


class Snapshot:
    def __init__(self):
        limit = int(os.environ["DEVELOPER_LIMIT"]) if "DEVELOPER_LIMIT" in os.environ else -1

        logger.info("Fetching developers...")
        self.developers = []
        developer_data = NetworkUtils.geode_paginated("/developers", limit=limit)

        with ThreadPoolExecutor(max_workers=int(os.environ["MAX_WORKER_THREADS"])) as executor:
            futures = [executor.submit(Developer, data) for data in developer_data]
            for future in tqdm(as_completed(futures), total=len(developer_data), desc="Developers"):
                self.developers.append(future.result())

    def save_to_json(self, is_monthly=False):
        data: str = jsonpickle.encode(self, unpicklable=False)  # pyright: ignore[reportAssignmentType]

        try:
            os.makedirs("data/")
        except FileExistsError:
            pass

        filename = "data/data.json"
        if is_monthly:
            filename = filename.replace("data.json", f"data-monthly-{datetime.now().month}.json")

        with open(filename, "w") as file:
            file.write(data)


def one_cycle(is_monthly=False):
    start = time.perf_counter()

    snapshot = Snapshot()
    snapshot.save_to_json(is_monthly)

    end = time.perf_counter()

    logger.info(f"Gathering data took {format_timespan(end - start)}!")

    if is_monthly:
        logger.info("This was a monthly data gathering! Come back in a month...")


if __name__ == "__main__":
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--monthly",
        help="Fetch Geode Wrapped data at the start of every month, automatically.",
        action="store_true",
    )
    parser.add_argument("--debug-log", help="Enables debug logs.", action="store_true")

    arguments = parser.parse_args()

    stdout_handler = logging.StreamHandler(sys.stdout)

    log_level = logging.DEBUG if arguments.debug_log else logging.INFO

    stdout_handler.setLevel(log_level)
    logging.getLogger("geode-wrapped").setLevel(log_level)
    logging.getLogger("urllib3").setLevel(log_level)
    logging.getLogger("requests").setLevel(log_level)
    logging.getLogger("apscheduler").setLevel(log_level)
    logging.getLogger("tzlocal").setLevel(log_level)

    file_handler = logging.FileHandler("debug.log", mode="w")
    file_handler.setLevel(logging.DEBUG)

    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)

    if arguments.monthly:
        logger.info("Scheduling monthly...")
        scheduler = BlockingScheduler()
        scheduler.add_job(lambda: one_cycle(True), "cron", day=4, hour=0, minute=0)
        scheduler.start()
    else:
        one_cycle()
