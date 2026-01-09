from fastapi import FastAPI, BackgroundTasks, Query, HTTPException
from fastapi.responses import PlainTextResponse, Response
import requests
import configparser
import os
import re
import yaml
import datetime
from collections import defaultdict

app = FastAPI()

# Force line buffering for stdout to ensure Docker logs appear immediately
import sys
sys.stdout.reconfigure(line_buffering=True)

# Cache Storage
SOURCE_CACHE = {} # {url: {'content': str, 'expires_at': datetime_obj}}
CACHE_TTL = 180   # 3 minutes


# ==========================================
# Helper Functions
# ==========================================

class LocationRenamer:
    def __init__(self):
        # 键: 标准地区代码, 值: 匹配关键字列表
        self.mappings = {
            "HK": ["Hong Kong", "HK", "HongKong", "香港"],
            "TW": ["Taiwan", "TW", "Taipei", "台湾"],
            "JP": ["Japan", "JP", "Tokyo", "Osaka", "日本"],
            "SG": ["Singapore", "SG", "新加坡"],
            "US": ["United States", "US", "America", "USA", "美国"],
            "KR": ["Korea", "KR", "Seoul", "韩国"],
            "UK": ["United Kingdom", "UK", "London", "英国"],
            "DE": ["Germany", "DE", "Berlin", "德国"],
            "FR": ["France", "FR", "Paris", "法国"],
            "CA": ["Canada", "CA", "Montreal", "Toronto", "加拿大"],
            "AU": ["Australia", "AU", "Sydney", "Melbourne", "澳大利亚"],
            "NL": ["Netherlands", "NL", "Amsterdam", "荷兰"],
            "IN": ["India", "IN", "Mumbai", "New Delhi", "印度"],
            "RU": ["Russia", "RU", "Moscow", "俄罗斯"],
            "TR": ["Turkey", "TR", "Istanbul", "土耳其"]
        }
        self.counters = defaultdict(int)
        self.fallback_counters = defaultdict(int)

    def get_name(self, original_name):
        # 提取前缀（如 [FP], [NFcloud]）
        prefix = ""
        name_without_prefix = original_name
        if original_name.startswith('[') and ']' in original_name:
            prefix_end = original_name.index(']')
            prefix = original_name[:prefix_end + 1]  # 包含 ]
            name_without_prefix = original_name[prefix_end + 1:].strip()
        
        name_lower = name_without_prefix.lower()
        matched_code = None
        found_match = False
        
        for code, keywords in self.mappings.items():
            for kw in keywords:
                # ASCII: word boundary check
                is_ascii = all(ord(c) < 128 for c in kw)
                if is_ascii:
                    pattern = r'(?i)\b' + re.escape(kw) + r'\b'
                    if re.search(pattern, name_without_prefix):
                        matched_code = code
                        found_match = True
                        break
                else:
                    # Non-ASCII: simple substring
                    if kw in name_without_prefix:
                        matched_code = code
                        found_match = True
                        break
            if found_match: break
        
        if matched_code:
            # 按 prefix + location 分组编号
            counter_key = f"{prefix}_{matched_code}"
            self.counters[counter_key] += 1
            node_name = f"{matched_code} {self.counters[counter_key]:02d}"
            # 添加前缀
            if prefix:
                return f"{prefix} {node_name}"
            return node_name
        else:
            # Fallback logic -> 'other'
            counter_key = f"{prefix}_other"
            self.counters[counter_key] += 1
            node_name = f"other {self.counters[counter_key]:02d}"
            if prefix:
                return f"{prefix} {node_name}"
            return node_name


def get_file_path(target, filename):
    return os.path.join(os.getcwd(), target, filename)

def get_beijing_time():
    utc_now = datetime.datetime.utcnow()
    beijing_time = utc_now + datetime.timedelta(hours=8)
    return beijing_time.strftime("%Y-%m-%d %H:%M:%S")

def filter_node_list(rule_str, node_names):
    # Syntax: {all filter=keyword1,keyword2 exclude=keyword3}
    match = re.search(r"\{all\s*(.*?)\}", rule_str)
    if not match: return node_names
    
    content = match.group(1)
    filters = []
    excludes = []
    
    parts = content.split()
    for p in parts:
        if p.startswith("filter="):
            filters = p.replace("filter=", "").split(",")
        if p.startswith("exclude="):
            excludes = p.replace("exclude=", "").split(",")
            
    res = []
    for node in node_names:
        # Exclude logic
        if any(ex in node for ex in excludes if ex): continue
        
        # Filter logic (OR logic: match any filter)
        if filters:
            if not any(f in node for f in filters if f): continue
            
        res.append(node)
    return res

def fetch_content_cached(url, headers=None, timeout=15):
    """
    带缓存的 URL 获取
    Key = URL (忽略 UA 差异以最大化共享，除非差异导致了解析失败，目前假设解析器足够健壮)
    """
    global SOURCE_CACHE
    now = datetime.datetime.now()
    
    # 1. Check Cache
    if url in SOURCE_CACHE:
        entry = SOURCE_CACHE[url]
        if entry['expires_at'] > now:
            print(f"[Cache] Hit for {url}")
            return entry['content'], 200
        else:
            print(f"[Cache] Expired for {url}")
            del SOURCE_CACHE[url]
    
    # 2. Fetch
    try:
        print(f"[Network] Fetching {url}")
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            # Update Cache
            expires = now + datetime.timedelta(seconds=CACHE_TTL)
            SOURCE_CACHE[url] = {
                'content': resp.text,
                'expires_at': expires
            }
            return resp.text, 200
        return None, resp.status_code
    except Exception as e:
        print(f"[Network] Error fetching {url}: {e}")
        return None, 500

# ==========================================
# 4. Gist 同步逻辑
# ==========================================

def get_merged_config(filename, target):
    """
    通用配置读取：
    优先级: config/{filename} ([target] > [Common]) > target/{filename}
    """
    # 1. 尝试读取统一配置 config/filename
    unified_path = get_file_path('config', filename)
    if os.path.exists(unified_path):
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(unified_path, encoding='utf-8')
            
            # 基础配置
            conf = {}
            if 'Common' in parser:
                conf.update(dict(parser['Common']))
            
            # 覆盖配置
            target_lower = target.lower()
            if target_lower in parser:
                conf.update(dict(parser[target_lower]))
            elif target.capitalize() in parser:
                conf.update(dict(parser[target.capitalize()]))
            
            if conf: return conf
        except Exception as e:
            print(f"Error reading config/{filename}: {e}")

    return None

def upload_to_gist(target: str, content_body: str):
    try:
        gist_conf = get_merged_config('gist.ini', target)
        if not gist_conf: 
            print(f"[{target}] Upload Skipped: Gist config not found")
            return
            
        token = gist_conf.get('token', '')
        gist_id = gist_conf.get('gist_id', '')
        filename = gist_conf.get('filename', '').strip() 
        raw_base = gist_conf.get('gist_raw_url_base', '')
        
        if not token or not gist_id or not filename:
            print(f"[{target}] Upload Skipped: Missing token/gist_id/filename")
            return
        
        timestamp = get_beijing_time()
        final_content = ""
        full_url = f"{raw_base}{filename}"
        
        if target == "surge":
            header = f"#!MANAGED-CONFIG {full_url} interval=28800 strict=true\n"
            comment = f"# Last Updated: {timestamp} (UTC+8)\n"
            final_content = header + comment + content_body
        else:
            comment = f"# Last Updated: {timestamp} (UTC+8)\n"
            final_content = comment + content_body

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        payload = {"files": { filename: { "content": final_content } }}
        
        print(f"[{target}] Uploading {filename} to Gist...")
        r = requests.patch(f"https://api.github.com/gists/{gist_id}", headers=headers, json=payload, timeout=20)
        
        if r.status_code == 200:
            print(f"[{target}] ✅ Gist Upload Success! (Time: {timestamp})")
        else:
            print(f"[{target}] ❌ Gist Upload FAILED: Status {r.status_code}")
            
    except Exception as e:
        print(f"[{target}] Gist Exception: {e}")

# ==========================================
# 2. Surge 配置处理逻辑
# ==========================================

def load_main_config(target):
    """加载主配置文件: 优先 config/config.ini"""
    # 1. 尝试统一配置
    unified_path = get_file_path('config', 'config.ini')
    if os.path.exists(unified_path):
        c = configparser.ConfigParser(interpolation=None)
        c.optionxform = str
        c.read(unified_path, encoding='utf-8')
        return c
    
    return None

def process_surge_config(target):
    # 读取基础配置
    conf = load_main_config(target)
    if not conf: return f"Error: config.ini not found"

    # 读取模板: 优先读取 config/surge_template.ini
    tpl_path = get_file_path('config', 'surge_template.ini')
    
    if not os.path.exists(tpl_path): return f"Error: template.ini not found"
    with open(tpl_path, 'r', encoding='utf-8') as f: template_body = f.read()

    # 初始化重命名器
    renamer = LocationRenamer()

    # 参数
    settings = conf['Settings'] if 'Settings' in conf else {}
    # 优先读取 user_agent_surge，否则读取 user_agent，最后默认
    custom_ua = settings.get('user_agent_surge', settings.get('user_agent', 'Surge/5'))
    exclude_keys = [k.strip() for k in settings.get('exclude_keywords', '').split(',') if k.strip()]
    headers = {"User-Agent": custom_ua}
    
    all_proxies = {}
    seen_fingerprints = set()

    # 抓取订阅
    if 'Sources' in conf:
        for src_name in conf['Sources']:
            raw_val = conf['Sources'][src_name]
            url, prefix = (raw_val.split('|', 1) + [""])[:2]
            url = url.strip(); prefix = prefix.strip()
            
            try:
                text_content, status_code = fetch_content_cached(url, headers=headers)
                if status_code == 200 and text_content:
                    # if " " not in text_content and len(text_content) > 10: (Removed redundant check, decode handles it)
                    if " " not in text_content and len(text_content) > 10:
                        try:
                            import base64
                            missing = len(text_content) % 4
                            if missing: text_content += '=' * (4 - missing)
                            decoded = base64.b64decode(text_content).decode('utf-8')
                            if "\n" in decoded or "\r" in decoded: text_content = decoded
                        except: pass

                    # Clash 解析 attempt
                    is_clash = False
                    if "proxies:" in text_content or "Proxy:" in text_content:
                        try:
                            clash_data = yaml.safe_load(text_content)
                            if clash_data and isinstance(clash_data, dict) and 'proxies' in clash_data:
                                is_clash = True
                                for proxy in clash_data['proxies']:
                                    p_name = proxy.get('name', '')
                                    p_type = proxy.get('type', '').lower()
                                    p_server = proxy.get('server', '')
                                    p_port = proxy.get('port', '')
                                    if not p_name or not p_server or not p_port: continue
                                    
                                    # 指纹
                                    fingerprint = f"{p_type}|{p_server}|{p_port}"
                                    if fingerprint in seen_fingerprints: continue
                                    seen_fingerprints.add(fingerprint)
                                    
                                    if any(k in p_name for k in exclude_keys): continue
                                    
                                    # 使用 LocationRenamer
                                    p_name = renamer.get_name(p_name)
                                    final_name = f"{prefix} {p_name}".strip() if prefix else p_name
                                    
                                    surge_line = ""
                                    if p_type == 'ss':
                                        cipher = proxy.get('cipher', '')
                                        password = proxy.get('password', '')
                                        surge_line = f"{final_name} = ss, {p_server}, {p_port}, encrypt-method={cipher}, password={password}"
                                    elif p_type == 'vmess':
                                        uuid = proxy.get('uuid', '')
                                        tls = "true" if proxy.get('tls') else "false"
                                        surge_line = f"{final_name} = vmess, {p_server}, {p_port}, username={uuid}, tls={tls}"
                                    elif p_type == 'trojan':
                                        password = proxy.get('password', '')
                                        sni = proxy.get('sni', '')
                                        skip_cert = "true" if proxy.get('skip-cert-verify') else "false"
                                        surge_line = f"{final_name} = trojan, {p_server}, {p_port}, password={password}, skip-cert-verify={skip_cert}"
                                        if sni: surge_line += f", sni={sni}"
                                    elif p_type == 'http':
                                        username = proxy.get('username', '')
                                        password = proxy.get('password', '')
                                        surge_line = f"{final_name} = http, {p_server}, {p_port}, username={username}, password={password}"
                                    elif p_type == 'socks5':
                                        username = proxy.get('username', '')
                                        password = proxy.get('password', '')
                                        surge_line = f"{final_name} = socks5, {p_server}, {p_port}, username={username}, password={password}"
                                    elif p_type == 'snell':
                                        psk = proxy.get('psk', '')
                                        version = proxy.get('version', '2')
                                        surge_line = f"{final_name} = snell, {p_server}, {p_port}, psk={psk}, version={version}"
                                    
                                    if surge_line:
                                        all_proxies[final_name] = surge_line
                        except: pass

                    # 文本解析 attempt
                    if not is_clash:
                        lines = text_content.split('\n')
                        in_proxy_section = False
                        has_proxy_section = any(l.strip().lower() == "[proxy]" for l in lines)
                        
                        for line in lines:
                            line = line.strip()
                            if not line: continue
                            if line.lower() == "[proxy]": in_proxy_section = True; continue
                            if line.startswith("[") and line.lower() != "[proxy]": in_proxy_section = False; continue
                            if has_proxy_section and not in_proxy_section: continue

                            if "=" in line and not line.startswith(("#", "//", ";")):
                                try:
                                    parts = line.split("=", 1)
                                    name = parts[0].strip()
                                    detail = parts[1].strip()
                                    
                                    d_parts = [x.strip() for x in detail.split(',')]
                                    if len(d_parts) < 3: continue
                                    
                                    p_type = d_parts[0].lower()
                                    p_server = d_parts[1]
                                    p_port = d_parts[2]
                                    
                                    # 指纹
                                    fingerprint = f"{p_type}|{p_server}|{p_port}"
                                    if fingerprint in seen_fingerprints: continue
                                    seen_fingerprints.add(fingerprint)

                                    if any(k in name for k in exclude_keys): continue
                                    
                                    # 使用 LocationRenamer
                                    name = renamer.get_name(name)
                                    final_name = f"{prefix} {name}".strip() if prefix else name
                                    
                                    all_proxies[final_name] = f"{final_name} = {detail}"
                                except: pass
            except Exception as e: print(f"[{target}] Error fetching {src_name}: {e}")

    # Manual (Surge) - 不进行重命名处理
    # 优先读取 Unified Manual
    manual_path = get_file_path('config', 'manual.ini')
    
    if os.path.exists(manual_path):
        try:
            with open(manual_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(("#", ";", "//", "[")): continue
                    if "=" in line:
                        m_name, m_detail = line.split("=", 1)
                        # 保持原名,不调用 renamer
                        all_proxies[m_name.strip()] = f"{m_name.strip()} = {m_detail.strip()}"
        except: pass

    # 生成链式代理节点 (Entry: JP/KR/TW, Exit: EXIT)
    # 为 EXIT 创建多个版本，每个使用不同的 JP/KR/TW 节点作为 underlying-proxy
    if 'EXIT' in all_proxies:
        us_ip_line = all_proxies['EXIT']
        # 解析 EXIT 配置
        if '=' in us_ip_line:
            parts = us_ip_line.split('=', 1)
            us_ip_detail = parts[1].strip()
            
            chain_proxies = {}
            for node_name in all_proxies.keys():
                # 跳过 EXIT 本身
                if node_name == 'EXIT':
                    continue
                # 只为 JP/KR/TW 节点创建链式代理
                if any(region in node_name for region in ['JP ', 'KR ', 'TW ']):
                    # 创建链式代理版本: 命名为 "完整节点名 Chain"
                    chain_name = f"{node_name} Chain"
                    chain_line = f"{chain_name} = {us_ip_detail}, underlying-proxy={node_name}"
                    chain_proxies[chain_name] = chain_line
            
            # 添加链式代理到 all_proxies
            all_proxies.update(chain_proxies)


    # 组装 - 按前缀分组并排序
    # 1. 按前缀分组
    from collections import defaultdict
    prefix_groups = defaultdict(list)
    
    for node_name, node_line in all_proxies.items():
        # 提取前缀
        prefix = ""
        if node_name.startswith('[') and ']' in node_name:
            prefix_end = node_name.index(']')
            prefix = node_name[:prefix_end + 1]  # 包含 ]
        prefix_groups[prefix].append((node_name, node_line))
    
    # 2. 对每个组内的节点按名称排序
    for prefix in prefix_groups:
        prefix_groups[prefix].sort(key=lambda x: x[0])
    
    # 3. 按前缀排序（无前缀的放最后）
    sorted_prefixes = sorted(prefix_groups.keys(), key=lambda x: (x == "", x))
    
    # 4. 组装 proxy_section
    proxy_lines = ["[Proxy]"]
    for prefix in sorted_prefixes:
        for node_name, node_line in prefix_groups[prefix]:
            proxy_lines.append(node_line)
    
    proxy_section = proxy_lines
    group_section = ["[Proxy Group]"]
    sorted_node_names = sorted(all_proxies.keys())
    
    if 'Groups' in conf:
        # 处理策略组覆盖逻辑 (例如 Auto_surge 覆盖 Auto)
        raw_groups = conf['Groups']
        final_groups = {}
        target_suffix = f"_{target}".lower()
        
        # 1. 加载基础组 (跳过带 explicitly 其他 target 后缀的)
        other_target = "clash" if target == "surge" else "surge"
        other_suffix = f"_{other_target}"
        
        for k, v in raw_groups.items():
            k_lower = k.lower()
            if k_lower.endswith(target_suffix): continue # 稍后处理
            if k_lower.endswith(other_suffix): continue  # 忽略其他目标的专用组
            final_groups[k] = v
            
        # 2. 加载特定覆盖
        for k, v in raw_groups.items():
            if k.lower().endswith(target_suffix):
                real_name = k[:-len(target_suffix)] # Auto_surge -> Auto
                final_groups[real_name] = v

        for g_name, g_rule in final_groups.items():
            match = re.search(r"\{all\s*(.*?)\}", g_rule)
            if match:
                filtered_list = filter_node_list(g_rule, sorted_node_names)
                nodes_str = ", ".join(filtered_list) if filtered_list else "DIRECT"
                final_rule = g_rule.replace(match.group(0), nodes_str)
                group_section.append(f"{g_name} = {final_rule}")
            else:
                group_section.append(f"{g_name} = {g_rule}")

    return template_body + "\n\n" + "\n".join(proxy_section) + "\n\n" + "\n".join(group_section)

# ==========================================
# 3. Clash 配置处理逻辑
# ==========================================

def process_clash_config(target):
    conf = load_main_config(target)
    if not conf: return "Error: config.ini not found"
    
    # 读取模板: 优先读取 config/clash_template.yaml
    tpl_path = get_file_path('config', 'clash_template.yaml')
    
    if not os.path.exists(tpl_path): return "Error: template.yaml not found"
    with open(tpl_path, 'r', encoding='utf-8') as f:
        clash_data = yaml.safe_load(f)

    # 初始化重命名器
    renamer = LocationRenamer()
            
    settings = conf['Settings'] if 'Settings' in conf else {}
    # 优先读取 user_agent_clash，否则读取 user_agent，最后默认
    custom_ua = settings.get('user_agent_clash', settings.get('user_agent', 'Clash/1.0'))
    
    exclude_keys = [k.strip() for k in settings.get('exclude_keywords', '').split(',') if k.strip()]
    
    all_proxies = []
    seen_fingerprints = set()
    seen_names = set() # Clash list checking is different, use set for names
    
    # 辅助：添加代理并去重
    def add_proxy(p_data, p_prefix="", skip_rename=False):
        p_name = p_data.get('name', '')
        if not p_name: return
        
        # 排除
        if any(k in p_name for k in exclude_keys): return
        
        # 重命名 (LocationRenamer) - 除非是 manual 节点
        if not skip_rename:
            p_name = renamer.get_name(p_name)
        if p_prefix: p_name = f"{p_prefix} {p_name}".strip()
        
        # 指纹去重
        p_type = p_data.get('type', '')
        p_server = p_data.get('server', '')
        p_port = p_data.get('port', '')
        
        if p_type and p_server and p_port:
            fingerprint = f"{p_type}|{p_server}|{p_port}"
            if fingerprint in seen_fingerprints: return
            seen_fingerprints.add(fingerprint)
        
        # URL/Map key collision check (Final safeguard, although renamer handles duplicates)
        # renamer handles duplicates for mapped locations, and fallback numbers,
        # but if prefix is added, or multiple sources cause collision?
        # Safe to keep a basic check or trust renamer?
        # With prefix, it might still collide if prefix is same.
        # Let's trust renamer ensures uniqueness for base name.
        # But allow safeguard if p_prefix modifies it?
        # Let's keep a simple safeguard loop just in case.
        final_name = p_name
        idx = 1
        while final_name in seen_names:
            idx += 1
            final_name = f"{p_name}_{idx}"
        
        seen_names.add(final_name)
        p_data['name'] = final_name
        all_proxies.append(p_data)

    if 'Sources' in conf:
        for src_name in conf['Sources']:
            raw_val = conf['Sources'][src_name]
            url, prefix = (raw_val.split('|', 1) + [""])[:2]
            url = url.strip(); prefix = prefix.strip()
            
            try:
                text_content, status_code = fetch_content_cached(url, headers={"User-Agent": custom_ua})
                if status_code == 200 and text_content:
                    # Base64 解码尝试
                    if " " not in text_content and len(text_content) > 10:
                        try:
                            import base64
                            missing_padding = len(text_content) % 4
                            if missing_padding: text_content += '=' * (4 - missing_padding)
                            decoded = base64.b64decode(text_content).decode('utf-8')
                            if "\n" in decoded or "\r" in decoded: text_content = decoded
                        except: pass

                    # 尝试 YAML 解析
                    is_yaml_success = False
                    if "proxies:" in text_content or "Proxy:" in text_content:
                        try:
                            data = yaml.safe_load(text_content)
                            if data and 'proxies' in data and isinstance(data['proxies'], list):
                                is_yaml_success = True
                                for proxy in data['proxies']:
                                    add_proxy(proxy, prefix)
                        except: pass
                    
                    # 尝试 Surge/文本 解析 (如果 YAML 失败)
                    if not is_yaml_success:
                        lines = text_content.split('\n')
                        in_proxy_section = False
                        has_proxy_section = any(l.strip().lower() == "[proxy]" for l in lines)
                        
                        for line in lines:
                            line = line.strip()
                            if not line: continue
                            if line.lower() == "[proxy]": in_proxy_section = True; continue
                            if line.startswith("[") and line.lower() != "[proxy]": in_proxy_section = False; continue
                            if has_proxy_section and not in_proxy_section: continue
                            
                            if "=" in line and not line.startswith(("#", "//", ";")):
                                try:
                                    # 解析 Surge 格式: Name = type, server, port, ...
                                    parts = line.split("=", 1)
                                    name = parts[0].strip()
                                    detail = parts[1].strip()
                                    
                                    d_parts = [x.strip() for x in detail.split(',')]
                                    if len(d_parts) < 3: continue
                                    
                                    # 构造 Clash Proxy Object
                                    p_type = d_parts[0].lower()
                                    p_server = d_parts[1]
                                    p_port = d_parts[2]
                                    
                                    proxy_obj = {
                                        "name": name,
                                        "type": p_type,
                                        "server": p_server,
                                        "port": p_port
                                    }
                                    
                                    # 填充额外参数 (简化版，仅提取核心参数以支持 Clash 输出)
                                    # 注意：从 Surge string 完美还原 Clash object 比较复杂，
                                    # 这里做尽力而为的转换，主要支持 ss, trojan, vmess, http, socks5
                                    # 提取 kv 参数
                                    kv_params = {}
                                    for item in d_parts[3:]:
                                        if "=" in item:
                                            k, v = item.split("=", 1)
                                            kv_params[k.strip()] = v.strip()
                                    
                                    if p_type == 'ss':
                                        proxy_obj['cipher'] = kv_params.get('encrypt-method', '')
                                        proxy_obj['password'] = kv_params.get('password', '')
                                    elif p_type == 'vmess':
                                        proxy_obj['uuid'] = kv_params.get('username', '')
                                        proxy_obj['cipher'] = 'auto'
                                        proxy_obj['tls'] = True if kv_params.get('tls', 'false') == 'true' else False
                                    elif p_type == 'trojan':
                                        proxy_obj['password'] = kv_params.get('password', '')
                                        if 'sni' in kv_params: proxy_obj['sni'] = kv_params['sni']
                                        if kv_params.get('skip-cert-verify') == 'true': proxy_obj['skip-cert-verify'] = True
                                    elif p_type in ['http', 'socks5']:
                                        proxy_obj['username'] = kv_params.get('username', '')
                                        proxy_obj['password'] = kv_params.get('password', '')
                                        # 支持 underlying-proxy -> dialer-proxy
                                        if 'underlying-proxy' in kv_params:
                                            proxy_obj['dialer-proxy'] = kv_params['underlying-proxy']
                                    elif p_type == 'snell':
                                        proxy_obj['psk'] = kv_params.get('psk', '')
                                        proxy_obj['version'] = kv_params.get('version', '2')
                                    elif p_type == 'hysteria2':
                                        # Hysteria2 参数转换
                                        proxy_obj['password'] = kv_params.get('password', '')
                                        if 'sni' in kv_params:
                                            proxy_obj['sni'] = kv_params['sni']
                                        # 只在需要跳过证书验证时添加该字段
                                        if kv_params.get('skip-cert-verify') == 'true':
                                            proxy_obj['skip-cert-verify'] = True
                                        if 'alpn' in kv_params:
                                            # alpn 可能是逗号分隔的列表
                                            alpn_val = kv_params['alpn']
                                            if ',' in alpn_val:
                                                proxy_obj['alpn'] = [a.strip() for a in alpn_val.split(',')]
                                            else:
                                                proxy_obj['alpn'] = [alpn_val]
                                        if 'obfs' in kv_params:
                                            proxy_obj['obfs'] = kv_params['obfs']
                                        if 'obfs-password' in kv_params:
                                            proxy_obj['obfs-password'] = kv_params['obfs-password']
                                        if 'download-bandwidth' in kv_params:
                                            try:
                                                proxy_obj['down'] = int(kv_params['download-bandwidth'])
                                            except: pass
                                        if 'upload-bandwidth' in kv_params:
                                            try:
                                                proxy_obj['up'] = int(kv_params['upload-bandwidth'])
                                            except: pass
                                        # 其他布尔参数
                                        if kv_params.get('udp-relay') == 'true':
                                            proxy_obj['udp'] = True
                                        if kv_params.get('tfo') == 'true':
                                            proxy_obj['fast-open'] = True


                                    add_proxy(proxy_obj, prefix)
                                except: pass

            except Exception as e:
                print(f"[{target}] Error fetching {src_name}: {e}")

    # Unified Manual Config (Surge Format) - 不进行重命名处理
    manual_path = get_file_path('config', 'manual.ini')

    if os.path.exists(manual_path):
        try:
             with open(manual_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith(("#", ";", "//", "[")): continue
                    if "=" in line:
                        try:
                            parts = line.split("=", 1)
                            name = parts[0].strip()
                            detail = parts[1].strip()
                            d_parts = [x.strip() for x in detail.split(',')]
                            if len(d_parts) < 3: continue
                            
                            p_type = d_parts[0].lower()
                            p_server = d_parts[1]
                            p_port = d_parts[2]
                            
                            proxy_obj = { "name": name, "type": p_type, "server": p_server, "port": p_port }
                            
                            kv_params = {}
                            for item in d_parts[3:]:
                                if "=" in item:
                                    k, v = item.split("=", 1)
                                    kv_params[k.strip()] = v.strip()
                            
                            if p_type == 'ss':
                                proxy_obj['cipher'] = kv_params.get('encrypt-method', '')
                                proxy_obj['password'] = kv_params.get('password', '')
                            elif p_type in ['http', 'socks5']:
                                proxy_obj['username'] = kv_params.get('username', '')
                                proxy_obj['password'] = kv_params.get('password', '')
                                if 'underlying-proxy' in kv_params:
                                    proxy_obj['dialer-proxy'] = kv_params['underlying-proxy']
                            
                            # skip_rename=True 保持原名
                            add_proxy(proxy_obj, skip_rename=True)
                        except: pass
        except Exception as e: print(f"Manual INI Error: {e}")
    
    # 生成链式代理节点 (Entry: JP/KR/TW, Exit: EXIT)
    # 为 EXIT 创建多个版本，每个使用不同的 JP/KR/TW 节点作为 dialer-proxy
    us_ip_proxy = None
    for p in all_proxies:
        if p.get('name') == 'EXIT':
            us_ip_proxy = p
            break
    
    if us_ip_proxy:
        import copy
        chain_proxies = []
        for proxy in all_proxies:
            node_name = proxy.get('name', '')
            # 跳过 EXIT 本身
            if node_name == 'EXIT':
                continue
            # 只为 JP/KR/TW 节点创建链式代理
            if any(region in node_name for region in ['JP ', 'KR ', 'TW ']):
                # 创建链式代理版本: 命名为 "完整节点名 Chain"
                chain_name = f"{node_name} Chain"
                
                chain_proxy = copy.deepcopy(us_ip_proxy)
                chain_proxy['name'] = chain_name
                chain_proxy['dialer-proxy'] = node_name
                chain_proxies.append(chain_proxy)
                # 同时添加到 seen_names 防止重复
                seen_names.add(chain_proxy['name'])
        
        # 添加链式代理到 all_proxies
        all_proxies.extend(chain_proxies)
    
    proxy_groups = []
    # 收集当前所有可用的节点名称
    all_current_node_names = [p['name'] for p in all_proxies]
    
    if 'Groups' in conf:
        # 处理策略组覆盖逻辑
        raw_groups = conf['Groups']
        final_groups = {}
        target_suffix = f"_{target}".lower()
        other_target = "surge" if target == "clash" else "clash"
        other_suffix = f"_{other_target}"
        
        for k, v in raw_groups.items():
            k_lower = k.lower()
            if k_lower.endswith(target_suffix): continue
            if k_lower.endswith(other_suffix): continue
            final_groups[k] = v
            
        for k, v in raw_groups.items():
            if k.lower().endswith(target_suffix):
                real_name = k[:-len(target_suffix)]
                final_groups[real_name] = v

        for g_name, g_rule in final_groups.items():
            # 1. 提取并移除 {all ...} 部分
            dynamic_nodes = []
            match = re.search(r"\{all\s*(.*?)\}", g_rule)
            if match:
                dynamic_nodes = filter_node_list(g_rule, all_current_node_names)
                # 将匹配到的部分替换为空，避免 split(',') 时被切碎
                g_rule_cleaned = g_rule.replace(match.group(0), "")
            else:
                g_rule_cleaned = g_rule
            
            # 2. 剩余部分按逗号分割，处理静态节点和参数
            parts = [p.strip() for p in g_rule_cleaned.split(',') if p.strip()]
            
            group_proxies = []
            
            # 先加入通过 filter 筛选出的节点
            if dynamic_nodes:
                group_proxies.extend(dynamic_nodes)
            
            group_obj = {"name": g_name}
            
            # 处理剩余部分：可能是类型(select/url-test)，也可能是静态节点名，也可能是参数(url=...)
            # 通常第一个是类型
            if parts:
                group_obj['type'] = parts[0]
                remaining_parts = parts[1:]
            else:
                group_obj['type'] = 'select' # default
                remaining_parts = []
            
            for part in remaining_parts:
                if "=" in part:
                    # 参数
                    k, v = part.split('=', 1)
                    k = k.strip(); v = v.strip()
                    if k == 'url': group_obj['url'] = v
                    if k == 'interval': 
                        try: group_obj['interval'] = int(v)
                        except: pass
                    if k == 'tolerance':
                        try: group_obj['tolerance'] = int(v)
                        except: pass
                else:
                    # 静态节点名称 (不再包含 {, }, =)
                    group_proxies.append(part)

            if not group_proxies: group_proxies.append("DIRECT")
            group_obj['proxies'] = group_proxies
            
            proxy_groups.append(group_obj)

    clash_data['proxies'] = all_proxies
    clash_data['proxy-groups'] = proxy_groups
    
    return yaml.dump(clash_data, allow_unicode=True, sort_keys=False)

# ==========================================
# 5. Web API 路由
# ==========================================

@app.get("/sync")
async def sync_config(background_tasks: BackgroundTasks, target: str = Query("surge")):
    if ".." in target or target.startswith("/"):
        raise HTTPException(status_code=400, detail=f"Invalid target '{target}'")

    content = ""
    content_type = "text/plain"

    if target == "clash":
        content = process_clash_config(target)
        content_type = "text/yaml"
    else:
        content = process_surge_config(target)
    
    background_tasks.add_task(upload_to_gist, target, content)

    timestamp = get_beijing_time()
    
    if target == "surge":
        try:
            conf = load_ini(target, 'config.ini')
            settings = conf['Settings'] if conf and 'Settings' in conf else {}
            base_url = settings.get('web_managed_url', 'http://127.0.0.1:8000/sync')
            sep = "&" if "?" in base_url else "?"
            
            header = f"#!MANAGED-CONFIG {base_url}{sep}target={target} interval=2880 strict=true\n"
            comment = f"# Last Updated: {timestamp} (UTC+8)\n"
            return PlainTextResponse(header + comment + content)
        except:
            return PlainTextResponse(content)
    else:
        # Clash 预览仅保留时间戳
        comment = f"# Last Updated: {timestamp} (UTC+8)\n"
        return Response(content=comment + content, media_type=content_type)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)