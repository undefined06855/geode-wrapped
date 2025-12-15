# Geode Wrapped

## Setup

1. Become index staff to get accurate mod versions and GitHub repository links
1. Clone the repository
1. Create a venv:
    - On Linux, run `python3 -m venv ./venv && source ./venv/bin/activate`
    - On Windows, run `python -m venv ./venv ; ./venv/Scripts/Activate.ps1`
1. Run `pip install -r requirements.txt`
1. Create a `.env` file based on the following:
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
1. Run `fetch.py` to fetch the data one time, or `fetch.py --monthly` to start a timer to fetch the data at the start of every month. (for more argument examples see `--help`)
1. Once the year is over, run `analyse.py` to analyse the information, and `serve.py` to start a web server!

## Contributing

Install development packages by running `pip install -r requirements_dev.txt`

Before committing, make sure to run
```
black . -t py314 -l 120
isort .
```

And if you want to add more packages, make sure to run
```
pip-compile --strip-extras
# or
pip-compile requirements_dev.in --strip-extras
```
(ideally using python 3.14 but whatever I don't really care if it's newer)

## License

MIT
