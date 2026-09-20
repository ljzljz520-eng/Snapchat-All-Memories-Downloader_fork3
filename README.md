# Snapchat-All-Memories-Downloader
This script will download all your Snapchat memories in bulk, **including the timestamp and geolocation**.

![demo](./demo.gif)


## Getting your Data
- Login to Snapchat: https://accounts.snapchat.com/
- Request your data: https://accounts.snapchat.com/accounts/downloadmydata
- Select the `Export your Memories` and `Export JSON Files` option and continue

![export configuration](https://github.com/user-attachments/assets/dfcdb6a0-e554-46e8-bdba-77fe41c88a03)

## Downloading your Memories
- Clone or [Download](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader/archive/refs/heads/main.zip) this Repository
- Extract the zip-file received from Snapchat in the same folder
- Run the script:
    - Requirements: Python3.10+
    - Install the required packages: 
	```
	pip install -r requirements.txt
	```
    - Run the script: 
    ```
    python main.py
    ```


### Optional Arguments
```
usage: main.py [-h] [-o OUTPUT] [-c CONCURRENT] [--no-exif] [--no-skip-existing]
               [--migrate-legacy] [json_file]

positional arguments:
json_file             Path to memories_history.json

options:
-h, --help            show this help message and exit
-o, --output OUTPUT   Output directory
-c, --concurrent CONCURRENT
                      Max concurrent downloads
--no-exif             Disable EXIF metadata
--no-skip-existing    Re-download even when the index already has the memory
--migrate-legacy      Register pre-upgrade flat jpg/mp4 files in place as
                      raw_source=unknown, then exit
```

### How downloads are stored
Each memory is kept in two explicit states inside the output directory:

- `.memories/raw/<xx>/<sha256>.<ext>` — the read-only, content-addressed
  **raw original**, byte-identical to the CDN response;
- `YYYY-MM-DD_HH-MM-SS.<ext>` — the **user-visible derived file**, published
  atomically only after the temp copy was fully written, (for JPEGs)
  re-parsed and verified to contain `DateTimeOriginal` and GPS tags.

`.memories/index.json` maps the source-record fingerprint to the raw digest,
raw/derived paths, provenance (`cdn`/`unknown`) and enrichment state
(`pending`/`success`/`not_required`/`failed`/`unknown`). Interrupted runs
resume from raw without re-requesting the CDN, and leftover temp files are
swept on startup. Files downloaded by older versions of the script are left
untouched by default; run with `--migrate-legacy` once to register them in
place with provenance `unknown`.

## Trouble Shooting
1. Make sure you get a fresh zip-file before running the script, links will expire over time
2. If you are missing the `memories_history.json` file, make sure you selected the right options in the export configuration
3. Still problems? please make a new [issue](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader/issues) 
