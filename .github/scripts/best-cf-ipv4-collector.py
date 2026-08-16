import ipaddress
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from curl_cffi import requests as cf_requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

if TYPE_CHECKING:
    from playwright.sync_api import Browser

# ==================== 配置 ====================
SOURCES: dict[str, str] = {
    'https://www.wetest.vip/page/cloudfront/address_v4.html': 'WeTest',
    'https://api.uouin.com/cloudflare.html': 'UOUIN',
    'https://bestcf.pages.dev/xinyitang3/ipv4.txt': 'Mia',
    'https://bestcf.pages.dev/tiancheng/all.txt': 'Tiancheng',
    'https://raw.githubusercontent.com/gslege/CloudflareIP/refs/heads/main/SG.txt': 'Gslege-SG',
    'https://bestcf.pages.dev/s5gy/hk.txt': 's5gy-hk',
    'https://bestcf.pages.dev/s5gy/jp.txt': 's5gy-jp',
    'https://raw.githubusercontent.com/gslege/CloudflareIP/refs/heads/main/US.txt': 'Gslege-US',
    'https://raw.githubusercontent.com/ymyuuu/IPDB/refs/heads/main/BestCF/bestcfv4.txt': 'IPDB',
    'https://vps789.com/openApi/cfIpApi': 'VPS789',
    'https://api.4ce.cn/api/bestCFIP': 'vvhan',
    'https://bestcf.pages.dev/luoli/all.txt': 'LuoLi',
}

PORT: str = '443'
HEADERS: dict[str, str] = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0',
}
IPV4_PATTERN: str = r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
OUTPUT_FILE: Path = Path('best-cf-ipv4.txt')
MAX_RETRIES: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0

# ==================== 工具函数 ====================
def _session() -> cf_requests.Session:
    session = cf_requests.Session(impersonate='chrome')
    session.headers.update(HEADERS)
    return session

def fetch(session: cf_requests.Session, url: str, timeout: int = 15) -> str:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_FACTOR ** attempt)
    assert last_err is not None
    raise last_err

def extract_ipv4(text: str) -> set[str]:
    ips: set[str] = set()
    for match in re.finditer(IPV4_PATTERN, text):
        try:
            ip = ipaddress.ip_address(match.group())
            ips.add(str(ip))
        except ValueError:
            continue
    return ips

def country_to_flag(code: str) -> str:
    if len(code) != 2 or code == 'XX':
        return ''
    return chr(ord(code[0]) - 65 + 0x1F1E6) + chr(ord(code[1]) - 65 + 0x1F1E6)

def lookup_country_single(ip: str) -> str:
    """单个 IP 查询（降级备用）"""
    try:
        with _session() as sess:
            resp = sess.get(
                f'http://ip-api.com/json/{ip}?fields=countryCode',
                timeout=10
            )
            data = resp.json()
            return data.get('countryCode', 'XX')
    except Exception:
        return 'XX'

def lookup_country_batch(ips: list[str]) -> dict[str, str]:
    """批量查询 IP 归属地，一次最多 100 个"""
    results: dict[str, str] = {}
    total = len(ips)
    
    for i in range(0, total, 100):
        batch = ips[i:i+100]
        print(f'  [batch] querying {i+1}-{min(i+100, total)}/{total}')
        
        url = 'http://ip-api.com/batch?fields=countryCode'
        try:
            with _session() as sess:
                resp = sess.post(url, json=batch, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                
                if not isinstance(data, list):
                    print(f'  [batch] unexpected response: {data}')
                    # 降级为单个查询
                    for ip in batch:
                        results[ip] = lookup_country_single(ip)
                else:
                    for idx, item in enumerate(data):
                        ip = batch[idx]
                        if isinstance(item, dict):
                            results[ip] = item.get('countryCode', 'XX')
                        else:
                            results[ip] = 'XX'
        except Exception as e:
            print(f'  [batch] error: {e}, falling back to single queries')
            for ip in batch:
                results[ip] = lookup_country_single(ip)
        
        # 批次之间稍作延迟
        time.sleep(0.5)
    
    return results

def beijing_timestamp() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')

# ==================== 浏览器渲染 ====================
_browser = None
_pw = None

def _get_browser() -> 'Browser':
    global _browser, _pw
    if sync_playwright is None:
        raise RuntimeError('playwright not installed; run: pip install playwright && playwright install chromium')
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
    return _browser

def fetch_rendered(url: str, timeout: int = 30000) -> str:
    context = _get_browser().new_context(user_agent=HEADERS['User-Agent'])
    page = context.new_page()
    try:
        page.goto(url, wait_until='networkidle', timeout=timeout)
        return page.content()
    finally:
        context.close()

# ==================== 核心逻辑 ====================
def collect_ips(session: cf_requests.Session) -> set[str]:
    all_ips: set[str] = set()
    tiers = [
        ('HTTP', lambda u: fetch(session, u)),
        ('Browser', fetch_rendered),
    ]
    for url, name in SOURCES.items():
        for label, fetcher in tiers:
            try:
                ips = extract_ipv4(fetcher(url))
            except Exception as e:
                print(f'  [{name}] {label} failed: {e}')
                continue
            if ips:
                all_ips.update(ips)
                print(f'  [{name}] {label}: {len(ips)} IPv4')
                break
            print(f'  [{name}] {label}: 0 IPv4, trying next tier')
        else:
            print(f'  [{name}] all fetchers failed')
    return all_ips

def enrich_locations(ips: set[str]) -> dict[str, str]:
    """批量查询 IP 地理位置"""
    ip_list = list(ips)
    print(f'  Querying {len(ip_list)} IPs in batches...')
    
    country_map = lookup_country_batch(ip_list)
    
    entries: dict[str, str] = {}
    for ip in ip_list:
        entries[f'{ip}:{PORT}'] = country_map.get(ip, 'XX')
    return entries

def main() -> int:
    print('Collecting Cloudflare IPs...\n')

    session = _session()

    all_ips = collect_ips(session)
    if not all_ips:
        print('No IPs collected, skip')
        return 1
    print(f'\n{len(all_ips)} unique IPv4')

    print('Querying locations...')
    entries = enrich_locations(all_ips)

    tmp = OUTPUT_FILE.with_suffix('.tmp')
    timestamp = beijing_timestamp()
    with tmp.open('w', encoding='utf-8') as f:
        f.write(f'#{len(entries)} bestips updated at {timestamp}\n')
        for ip_port, location in entries.items():
            f.write(f'{ip_port}#{location} {country_to_flag(location)}\n')
    tmp.replace(OUTPUT_FILE)
    print(f'\n{len(entries)} IPs written to {OUTPUT_FILE}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
