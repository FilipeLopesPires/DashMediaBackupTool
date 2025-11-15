# Dash Media Backup Tool

A Python script to download all files referenced by MPEG-DASH MPD manifests, preserving the domain-relative folder structure locally.

## Features

- Downloads all segments, init segments, and media files from DASH manifests
- Preserves original folder structure
- Parallel downloads with configurable concurrency
- Retry mechanism for failed downloads
- Filtering by representation ID and MIME type
- Domain restriction for security
- Dry-run mode to preview downloads

## Installation

1. Clone or download this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Basic Usage

```bash
python dash-media-backup-tool.py --manifest https://example.com/path/to/manifest.mpd --out ./downloaded
```

### Options

- `--manifest` - URL or path to the MPD manifest file (required)
- `--out` - Output directory (default: `dash_downloads`)
- `--filter-repr-id` - Only download representations with specific IDs (repeatable)
- `--filter-mime` - Only download specific MIME types (repeatable)
- `--concurrency` - Number of parallel downloads (default: 8)
- `--retry` - Number of retries per file (default: 3)
- `--timeout` - Per-request timeout in seconds (default: 30)
- `--dry-run` - Parse and list URLs without downloading
- `--headers` - Extra HTTP headers (repeatable)
- `--user-agent` - Custom User-Agent string
- `--only-domain` - Restrict downloads to specific domain
- `--verbose` - Enable detailed logging

### Examples

Download all files from a manifest:
```bash
python dash-media-backup-tool.py --manifest https://example.com/video.mpd --out ./video_files
```

Download only video segments:
```bash
python dash-media-backup-tool.py --manifest https://example.com/video.mpd --filter-mime video/mp4
```

Preview what would be downloaded:
```bash
python dash-media-backup-tool.py --manifest https://example.com/video.mpd --dry-run
```

Download with custom headers:
```bash
python dash-media-backup-tool.py --manifest https://example.com/video.mpd --headers "Authorization: Bearer TOKEN"
```

## Requirements

- Python 3.6+
- requests library

## Notes

- For number-based SegmentTemplate manifests, set the `DASH_SEGMENT_COUNT` environment variable
- The tool automatically handles BaseURL resolution and template expansion
- Failed downloads are retried with exponential backoff

## Project Background

This tool was created out of personal necessity when existing open-source solutions didn't meet specific requirements. While this script provides a lightweight Python solution for basic DASH media downloading, users with more complex needs may benefit from more feature-rich alternatives such as [dash-mpd-cli](https://github.com/emarsden/dash-mpd-cli), a comprehensive Rust-based tool with advanced features like muxing, subtitle support, and DRM handling. For production use or complex streaming scenarios, consider evaluating this or other alternatives first.

## Author

I'm the sole author of this repository. For further information, feel free to reach out.
