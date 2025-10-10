#!/usr/bin/env python3
"""
dash-media-backup-tool.py
Download every file referenced by a MPEG-DASH MPD manifest (segments, init segments, etc.),
preserving the domain-relative folder structure locally.

Usage:
    python dash-media-backup-tool.py --manifest https://example.com/path/to/manifest.mpd --out ./downloaded

Options:
    --filter-repr-id   Only download representations whose @id matches this (repeatable)
    --filter-mime      Only download adaptation sets/representations matching mimeType (repeatable)
    --concurrency      Number of parallel downloads (default 8)
    --retry            Number of retries per file (default 3)
    --timeout          Per-request timeout seconds (default 30)
    --dry-run          Parse & list URLs without downloading
    --headers          Extra HTTP headers, e.g. --headers "Authorization: Bearer TOKEN" (repeatable)
    --user-agent       Custom User-Agent string
    --only-domain      Restrict downloads to this domain (safety check)
    --verbose          Chatty logging
"""

import argparse
import concurrent.futures
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

try:
    import requests
except ImportError:
    print("This script requires 'requests'. Install it with: pip install requests")
    sys.exit(1)

import xml.etree.ElementTree as ET

MPD_NS = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}

@dataclass
class DownloadItem:
    url: str
    relpath: str

# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default="dash_downloads")
    ap.add_argument("--filter-repr-id", action="append")
    ap.add_argument("--filter-mime", action="append")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--retry", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headers", action="append")
    ap.add_argument("--user-agent", default="dash-downloader/1.1")
    ap.add_argument("--only-domain")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()

# ---------------------------------------------------------------------------

def merge_headers(args) -> Dict[str, str]:
    headers = {"User-Agent": args.user_agent}
    if args.headers:
        for h in args.headers:
            if ":" not in h:
                print(f"Warning: bad header: {h}", file=sys.stderr)
                continue
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers

def read_manifest(manifest: str, headers: Dict[str, str], timeout: int) -> Tuple[str, bytes]:
    if re.match(r"^https?://", manifest, re.I):
        r = requests.get(manifest, headers=headers, timeout=timeout)
        r.raise_for_status()
        return manifest, r.content
    else:
        path = Path(manifest).resolve()
        return path.as_uri(), path.read_bytes()

def join_url(base: str, part: str) -> str:
    return urllib.parse.urljoin(base, part)

def _clean_baseurl_text(text: Optional[str]) -> Optional[str]:
    """
    Ignore 'no-op' BaseURL values that break relative resolution, e.g. '' or '/'.
    Keep absolute URLs and meaningful relative paths.
    """
    if text is None:
        return None
    t = text.strip()
    if t == "" or t == "/":
        return None
    return t

def get_all_baseurls(elem: ET.Element) -> List[str]:
    # Collect BaseURL texts, dropping empty and '/' which would reset to domain root.
    urls = []
    for b in elem.findall('mpd:BaseURL', MPD_NS):
        t = _clean_baseurl_text(b.text)
        if t is not None:
            urls.append(t)
    # If nothing meaningful at this level, return [''] so upper level base is preserved
    return urls or ['']

def ensure_relpath_from_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    rel = p.path.lstrip('/')
    if p.query:
        rel = os.path.join(rel, urllib.parse.quote_plus(p.query))
    return rel

def add_item(items: Dict[str, "DownloadItem"], url: str):
    items[url] = DownloadItem(url, ensure_relpath_from_url(url))

def expand_media_template(pat: str, rep: ET.Element, number=None, time=None) -> str:
    out = pat
    if "$Number" in out:
        if number is None:
            raise RuntimeError("Template uses $Number$ but number=None")
        out = re.sub(r"\$Number(%0(\d+)d)?\$", 
                     lambda m: f"{number:0{m.group(2)}d}" if m.group(2) else str(number), out)
    if "$Time$" in out:
        if time is None:
            raise RuntimeError("Template uses $Time$ but time=None")
        out = out.replace("$Time$", str(time))
    if "$RepresentationID$" in out:
        out = out.replace("$RepresentationID$", rep.get('id') or '')
    if "$Bandwidth$" in out:
        out = out.replace("$Bandwidth$", rep.get('bandwidth') or '')
    return out

def initialize_url_pattern(pat: str, rep: ET.Element) -> str:
    return (pat
        .replace("$RepresentationID$", rep.get('id') or '')
        .replace("$Bandwidth$", rep.get('bandwidth') or ''))

# ---------------------------------------------------------------------------

def effective_base_urls_hierarchy(root: ET.Element, base: str):
    """
    Build effective base URLs for (Representation, AdaptationSet, effective_base).
    We 'stack' BaseURL from MPD -> Period -> AdaptationSet -> Representation,
    but ignore BaseURL values that are '' or '/' (no-ops that otherwise reset to host root).
    """
    out = []
    mpd_bases = [join_url(base, b) for b in get_all_baseurls(root)]
    for period in root.findall('mpd:Period', MPD_NS):
        p_bases = [join_url(m, b) for m in mpd_bases for b in get_all_baseurls(period)]
        for adp in period.findall('mpd:AdaptationSet', MPD_NS):
            a_bases = [join_url(p, b) for p in p_bases for b in get_all_baseurls(adp)]
            for rep in adp.findall('mpd:Representation', MPD_NS):
                r_bases = [join_url(a, b) for a in a_bases for b in get_all_baseurls(rep)]
                for eff in r_bases:
                    out.append((rep, adp, eff))
    return out

def matches_filters(rep, adp, args):
    if args.filter_repr_id and (rep.get('id') not in args.filter_repr_id):
        return False
    if args.filter_mime:
        mime = rep.get('mimeType') or adp.get('mimeType') or ''
        if not any(mime.lower().startswith(m.lower()) for m in args.filter_mime):
            return False
    return True

# ---------------------------------------------------------------------------

def parse_segment_template(rep, adp, base, items):
    st = rep.find('mpd:SegmentTemplate', MPD_NS) or adp.find('mpd:SegmentTemplate', MPD_NS)
    if st is None:
        return
    init = st.get('initialization')
    media = st.get('media')
    if init:
        add_item(items, join_url(base, initialize_url_pattern(init, rep)))
    if not media:
        return
    timeline = st.find('mpd:SegmentTimeline', MPD_NS)
    start_number = int(st.get('startNumber') or '1')
    if timeline is not None:
        current_time = None
        for s in timeline.findall('mpd:S', MPD_NS):
            r = int(s.get('r') or '0')
            d = s.get('d')
            t = s.get('t')
            if t is not None:
                current_time = int(t)
            elif current_time is None:
                current_time = 0
            for _ in range(r + 1):
                seg = expand_media_template(media, rep, time=current_time)
                add_item(items, join_url(base, seg))
                if d is not None:
                    current_time += int(d)
    else:
        count_env = os.getenv("DASH_SEGMENT_COUNT")
        if not count_env:
            raise RuntimeError("Need DASH_SEGMENT_COUNT for number-based SegmentTemplate")
        count = int(count_env)
        for i in range(start_number, start_number + count):
            seg = expand_media_template(media, rep, number=i)
            add_item(items, join_url(base, seg))

def parse_segment_list(rep, adp, base, items):
    sl = rep.find('mpd:SegmentList', MPD_NS) or adp.find('mpd:SegmentList', MPD_NS)
    if sl is None:
        return
    init = sl.find('mpd:Initialization', MPD_NS)
    if init is not None and init.get('sourceURL'):
        add_item(items, join_url(base, init.get('sourceURL')))
    for su in sl.findall('mpd:SegmentURL', MPD_NS):
        media = su.get('media')
        if media:
            add_item(items, join_url(base, media))

# ---------------------------------------------------------------------------

def collect_items(mpd_xml: bytes, base_url: str, args):
    root = ET.fromstring(mpd_xml)
    items = {}
    for rep, adp, eff in effective_base_urls_hierarchy(root, base_url):
        if not matches_filters(rep, adp, args):
            continue
        parse_segment_list(rep, adp, eff, items)
        parse_segment_template(rep, adp, eff, items)
        # If nothing explicit, treat eff as a direct file (rare)
        path = urllib.parse.urlparse(eff).path
        if not items and os.path.splitext(path)[1]:
            add_item(items, eff)
    return list(items.values())

# ---------------------------------------------------------------------------

def check_domain(url: str, only_domain: Optional[str]) -> bool:
    if not only_domain:
        return True
    return urllib.parse.urlparse(url).hostname == only_domain

def download_one(item: DownloadItem, outdir: str, headers: Dict[str, str],
                 timeout: int, retries: int, only_domain: Optional[str], verbose: bool):
    if not check_domain(item.url, only_domain):
        return (item.url, False, "wrong domain")
    dest = os.path.join(outdir, item.relpath)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    attempt = 0
    while attempt <= retries:
        try:
            with requests.get(item.url, headers=headers, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(262144):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dest)
            return (item.url, True, None)
        except Exception as e:
            attempt += 1
            if attempt > retries:
                return (item.url, False, str(e))
            if verbose:
                print(f"Retry {attempt}/{retries}: {item.url} ({e})")
            time.sleep(min(2 ** attempt, 10))

# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    headers = merge_headers(args)
    base_url, xml = read_manifest(args.manifest, headers, args.timeout)
    items = collect_items(xml, base_url, args)
    print(f"Discovered {len(items)} file(s).")
    if args.dry_run:
        for it in items[:50]:
            print(it.url)
        if len(items) > 50:
            print(f"... and {len(items)-50} more")
        return
    os.makedirs(args.out, exist_ok=True)
    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(download_one, it, args.out, headers, args.timeout,
                             args.retry, args.only_domain, args.verbose)
                   for it in items]
        for f in concurrent.futures.as_completed(futures):
            url, success, err = f.result()
            if success:
                ok += 1
                if args.verbose: print("[OK]", url)
            else:
                fail += 1
                print("[FAIL]", url, "->", err, file=sys.stderr)
    print(f"Done. Success: {ok}, Failed: {fail}. Output: {os.path.abspath(args.out)}")

if __name__ == "__main__":
    main()
