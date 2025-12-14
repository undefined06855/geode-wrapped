# Geode Wrapped

## Setup

1. Become index staff to get accurate mod versions (or alternatively convince everyone on the index to add you as a co-owner of their mod)
1. Create a venv (`python3 -m venv ./venv && source ./venv/bin/activate # or platform-specific alternative`) and run `pip install -r requirements.txt`
1. Create a .env file based on the following:
```
GITHUB_AUTH_TOKEN=<yo token>
GEODE_API_TOKEN=<yo geode token, copied from cli's config.json>
GEODE_API_ENDPOINT=https://api.geode-sdk.org/v1
GITHUB_API_ENDPOINT=https://api.github.com
FETCH_GITHUB_REPO_DATA=true # will make the output data extremely large
DEVELOPER_LIMIT=10 # optional, for debugging a smaller set of developers
MAX_WORKER_THREADS=20
```
(or just set the environment variables)
1. Run `fetch.py` to fetch the data one time, or `fetch.py --monthly` to start a timer to fetch the data at the start of every month.
