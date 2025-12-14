from enum import Enum
from dotenv import load_dotenv
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from humanfriendly import format_timespan
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
import jsonpickle
import os
import requests
import re
import time
import sys

class GeodeVersionState(str, Enum):
    pending = "pending"
    rejected = "rejected"
    unlisted = "unlisted"
    verified = "verified"

class NetworkError(Exception):
    pass


original_print = print
def print(string: Any):
    original_print(string)

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
    def parse_github_url(url: str) -> tuple[str, str] | None:
        if "github.com" not in url:
            return None

        match = re.search(r"github\.com[:/](?P<user>[^/]+)/(?P<repo>[^/]+)(?:\.git)?", url)
        if not match:
            return None

        username = match.group('user')
        repo = match.group('repo').replace('.git', '')
        return username, repo

    @staticmethod
    def github_auth_headers():
        return {
            "Authorization": f"Bearer {os.environ["GITHUB_API_TOKEN"]}"
        }

    @staticmethod
    def geode_auth_headers():
        return {
            "Authorization": f"Bearer {os.environ["GEODE_API_TOKEN"]}"
        }

class NetworkUtils:
    @staticmethod
    def raw(full_url: str, params_or_json: dict[str, Any] = {}, headers: dict[str, str] = {}, method="GET"):
        request = requests.Request(
            method,
            full_url,
            params=params_or_json if method == "GET" else None,
            json=params_or_json if method == "POST" else None,
            headers=headers
        )
        prepared = request.prepare()

        print(f"[INFO] Fetching {prepared.url}")

        session = requests.Session()
        response = session.send(prepared)

        if response.status_code != 200:
            raise NetworkError(f"{request.url} response code was not 200!")

        json = response.json()

        if str(type(json)) == "<class 'list'>":
            json = { "__data": json }

        link_header = response.headers.get("Link")
        if link_header:
            for part in link_header.split(", "):
                url, rel = part.split(";")
                if rel.strip() == "rel=\"next\"":
                    json["__internal_github_next_url"] = url.strip()[1:-1]
                    break

        return json

    @staticmethod
    def geode(endpoint: str, data: dict[str, Any] = {}):
        url_base = os.environ["GEODE_API_ENDPOINT"]

        json_data = NetworkUtils.raw(f"{url_base}{endpoint}", params_or_json=data, headers=Utils.geode_auth_headers())

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
            raise NetworkError(f"Error when fetching {endpoint} from GitHub: {json_data["message"]} (see {json_data["documentation_url"]})")

        return json_data

    @staticmethod
    def github_paginated(endpoint: str, required_data: str, data: dict[str, Any]={}):
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
        latest_commit_sha = NetworkUtils.github(f"/repos/{full_name}/branches/{json_data["default_branch"]}")["commit"]["sha"]
        repo_tree = NetworkUtils.github(f"/repos/{full_name}/git/trees/{latest_commit_sha}", { "recursive": 1 })

        if repo_tree["truncated"]:
            # what the fuck
            # not even globed gets truncated
            print(f"[WARN] Repo {full_name} is too large and so the file count will be incorrect! Manually traversing file trees may be implemented in the future.")

        self.file_count = len([ file for file in repo_tree["tree"] if file["type"] == "blob"])
        self.total_action_runs = len([ run for run in actions_data if run["status"] == "completed" ])
        self.failed_action_runs = len([ run for run in actions_data if run["conclusion"] == "failure" ])

        self.commits: list[DeveloperGithubCommit] = []

        cursor = None
        while True:
            json = {
                "query": REPO_INFO_GRAPHQL,
                "variables": {
                    "owner": name,
                    "name": repo,
                    "cursor": cursor
                }
            }

            data = NetworkUtils.raw(f"{os.environ["GITHUB_API_ENDPOINT"]}/graphql", json, Utils.github_auth_headers(), "POST")

            if "errors" in data:
                raise NetworkError(f"GraphQL Error!\n{jsonpickle.dumps(data["errors"], indent=4)}")

            rateLimitInfo = data["data"]["rateLimit"]
            repositoryInfo = data["data"]["repository"]

            print(f"[INFO] GraphQL remaining: {rateLimitInfo["remaining"]}")

            if rateLimitInfo["remaining"] < 20:
                print(f"[WARN] GraphQL remaining count very low! ({rateLimitInfo["remaining"]}) (resets in {datetime.fromisoformat(rateLimitInfo["resetAt"]).timestamp() - time.time()})")

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
        self.versions = [ DeveloperGeodeModVersion(data) for data in NetworkUtils.geode(f"/mods/{json_data["id"]}")["versions"] ]
        self.downloads: int = json_data["download_count"]
        self.featured: bool = json_data["featured"]
        self.developer_count = len(json_data["developers"])

        latest_version_info = NetworkUtils.geode(f"/mods/{json_data["id"]}/versions/latest")
        self.dependency_count = len(latest_version_info["dependencies"])
        self.creation_date = latest_version_info["created_at"]

        # TODO: get github link from release url

        if os.environ["FETCH_GITHUB_REPO_DATA"] != "true":
            return

        if json_data["links"] == None:
            self.github_repo = None
            return

        source_link = json_data["links"]["source"]

        if source_link == None:
            self.github_repo = None
            return

        if "github" not in source_link:
            self.github_repo = None
            return

        parsed = Utils.parse_github_url(source_link)
        if not parsed:
            return

        username, repo_name = parsed

        self.github_repo = DeveloperGithubRepository(NetworkUtils.github(f"/repos/{username}/{repo_name}"))

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

        self.mods = [ DeveloperGeodeMod(data) for data in NetworkUtils.geode_paginated("/mods", { "developer": self.username }) ]

        try:
            github_data = NetworkUtils.github(f"/user/{self.github_id}")
            self.github_data = DeveloperGithubInfo(github_data)
        except NetworkError:
            print(f"[WARN] GitHub account associated with {self.username} does not exist!")

class Snapshot:
    def __init__(self):
        limit = int(os.environ["DEVELOPER_LIMIT"]) if "DEVELOPER_LIMIT" in os.environ else -1

        self.developers = []
        developer_data = NetworkUtils.geode_paginated("/developers", limit=limit)
        with ThreadPoolExecutor(max_workers=int(os.environ["MAX_WORKER_THREADS"])) as executor:
            futures = [ executor.submit(Developer, data) for data in developer_data ]
            for future in as_completed(futures):
                self.developers.append(future.result())

    def save_to_json(self, is_monthly = False):
        data: str = jsonpickle.encode(self, unpicklable=False) # pyright: ignore[reportAssignmentType]

        try:
            os.makedirs("data/")
        except FileExistsError:
            pass

        filename = "data/data.json"
        if is_monthly:
            filename = filename.replace("data.json", f"data-monthly-{datetime.now().month}.json")

        with open(filename, "w") as file:
            file.write(data)

def one_cycle(is_monthly = False):
    start = time.perf_counter()

    snapshot = Snapshot()
    snapshot.save_to_json(is_monthly)

    end = time.perf_counter()

    print(f"[INFO] Gathering data took {format_timespan(end - start)}!")

    if is_monthly:
        print("[INFO] This was a monthly data gathering! Come back in a month...")

if __name__ == "__main__":
    load_dotenv()

    if len(sys.argv) <= 1:
        one_cycle()
    elif sys.argv[1] == "--monthly":
        print("[INFO] Scheduling monthly...")
        scheduler = BlockingScheduler()
        scheduler.add_job(lambda: one_cycle(True), "cron", day=1, hour=0, minute=0)
        scheduler.start()
    else:
        print("[WARN] Unknown command line arguments!")
