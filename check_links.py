#!/usr/bin/env python3
"""
检测 awesome-openclaw-skills README.md 中链接的有效性。
使用 HEAD 请求检测状态码，支持通过 GITHUB_TOKEN 环境变量提高 GitHub API 并发限制。
"""

import argparse
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Optional
from urllib.parse import urlparse


@dataclass
class LinkResult:
    """链接检测结果"""
    name: str
    url: str
    line_num: int
    original_line: str
    status_code: Optional[int]
    error: Optional[str]
    is_valid: bool


def extract_links_from_readme(filepath: str) -> list[tuple[str, str, int, str]]:
    """
    从 README.md 中提取所有技能链接。
    
    返回: [(skill_name, url, line_num, original_line), ...]
    """
    pattern = re.compile(r'-\s+\[([^\]]+)\]\((https://github\.com/openclaw/skills/[^\)]+)\)')
    
    links = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            match = pattern.search(line)
            if match:
                name, url = match.groups()
                links.append((name, url, line_num, line.rstrip('\n')))
    
    return links


def check_link(name: str, url: str, github_token: Optional[str], timeout: int = 10) -> tuple[Optional[int], Optional[str], bool]:
    """
    使用 HEAD 请求检测单个链接的有效性。
    
    对于 GitHub 链接，使用 GITHUB_TOKEN 进行认证以提高 API 限制。
    GitHub API 限制：
    - 未认证：60 次/小时
    - 认证后：5000 次/小时
    
    返回: (status_code, error_msg, is_valid)
    """
    # 构建请求
    parsed = urlparse(url)
    
    # 将 github.com 链接转换为 API 调用以获取更准确的状态
    # 例如：https://github.com/openclaw/skills/tree/main/skills/xxx/SKILL.md
    # 转换为：https://api.github.com/repos/openclaw/skills/contents/skills/xxx/SKILL.md?ref=main
    
    is_github = parsed.netloc == 'github.com'
    
    if is_github:
        # 解析 GitHub URL 路径
        path_parts = parsed.path.split('/')
        # /openclaw/skills/tree/main/skills/author/skill-name/SKILL.md
        if len(path_parts) >= 6 and path_parts[3] == 'tree':
            repo_owner = path_parts[1]
            repo_name = path_parts[2]
            branch = path_parts[4]
            file_path = '/'.join(path_parts[5:])
            
            # 构建 GitHub API URL
            api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents/{file_path}?ref={branch}"
            check_url = api_url
        else:
            check_url = url
    else:
        check_url = url
    
    # 创建请求
    req = urllib.request.Request(check_url, method='HEAD')
    
    # 设置请求头
    req.add_header('User-Agent', 'awesome-openclaw-skills-link-checker/1.0')
    
    if is_github and github_token:
        req.add_header('Authorization', f'token {github_token}')
        # GitHub API 需要 Accept 头
        req.add_header('Accept', 'application/vnd.github.v3+json')
    
    # 创建 SSL 上下文
    ssl_context = ssl.create_default_context()
    
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            # 对于 GitHub API，HEAD 请求可能不支持，需要处理
            if isinstance(response, HTTPResponse):
                status_code = response.status
            else:
                status_code = 200
            
            is_valid = 200 <= status_code < 400
            
            return (status_code, None, is_valid)
    
    except urllib.error.HTTPError as e:
        status_code = e.code
        error_msg = None
        
        if status_code == 404:
            error_msg = "Not Found"
            is_valid = False
        elif status_code == 403:
            error_msg = "Forbidden (rate limited?)"
            # 速率限制表示资源存在，只是暂时无法访问
            is_valid = True
        elif status_code == 429:
            error_msg = "Too Many Requests"
            # 速率限制表示资源存在，只是暂时无法访问
            is_valid = True
        else:
            error_msg = f"HTTP {status_code}"
            is_valid = False
        
        return (status_code, error_msg, is_valid)
    
    except urllib.error.URLError as e:
        return (None, f"URL Error: {e.reason}", False)
    
    except TimeoutError:
        return (None, "Timeout", False)
    
    except Exception as e:
        return (None, f"Error: {str(e)}", False)


def check_all_links(
    links: list[tuple[str, str, int, str]],
    github_token: Optional[str],
    max_workers: int = 10,
    rate_limit_delay: float = 0.1
) -> list[LinkResult]:
    """
    并发检测所有链接。
    
    参数:
        links: [(name, url, line_num, original_line), ...]
        github_token: GitHub 个人访问令牌
        max_workers: 最大并发数
        rate_limit_delay: 每次请求之间的延迟（秒）
    """
    results = []
    total = len(links)
    
    print(f"开始检测 {total} 个链接...")
    print(f"并发数: {max_workers}")
    print(f"GITHUB_TOKEN: {'已设置' if github_token else '未设置 (限制: 60次/小时)'}")
    print("-" * 60)
    
    def check_with_delay(link_tuple):
        name, url, line_num, original_line = link_tuple
        status_code, error, is_valid = check_link(name, url, github_token)
        time.sleep(rate_limit_delay)  # 添加延迟以避免触发速率限制
        return LinkResult(
            name=name,
            url=url,
            line_num=line_num,
            original_line=original_line,
            status_code=status_code,
            error=error,
            is_valid=is_valid
        )
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_with_delay, link): link
            for link in links
        }
        
        completed = 0
        for future in as_completed(futures):
            completed += 1
            result = future.result()
            results.append(result)
            
            # 显示进度（始终打印 URL）
            status_icon = "✓" if result.is_valid else "✗"
            if result.is_valid:
                print(f"[{completed}/{total}] {status_icon} {result.name}")
                print(f"    {result.url}")
            else:
                error_info = result.error or f"HTTP {result.status_code}"
                print(f"[{completed}/{total}] {status_icon} {result.name} - {error_info}")
                print(f"    {result.url}")
    
    return results


def delete_invalid_lines(readme_path: str, results: list[LinkResult]) -> int:
    """
    删除 README.md 中无效链接所在的行。
    
    返回: 删除的行数
    """
    # 收集需要删除的行号
    invalid_lines = {r.line_num for r in results if not r.is_valid}
    
    if not invalid_lines:
        return 0
    
    # 读取所有行
    with open(readme_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 过滤掉无效行
    new_lines = [
        line for line_num, line in enumerate(lines, 1)
        if line_num not in invalid_lines
    ]
    
    # 写回文件
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    return len(invalid_lines)


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='检测 README.md 中链接的有效性')
    parser.add_argument('--delete', action='store_true', help='删除无效链接所在的行')
    args = parser.parse_args()
    
    # 获取 README.md 路径
    script_dir = os.path.dirname(os.path.abspath(__file__))
    readme_path = os.path.join(script_dir, "README.md")
    
    if not os.path.exists(readme_path):
        print(f"错误: 找不到 README.md 文件: {readme_path}")
        sys.exit(1)
    
    # 获取 GITHUB_TOKEN
    github_token = os.environ.get("GITHUB_TOKEN")
    
    # 提取链接
    print(f"正在读取 {readme_path}...")
    links = extract_links_from_readme(readme_path)
    print(f"找到 {len(links)} 个链接")
    print()
    
    if not links:
        print("没有找到任何链接")
        sys.exit(0)
    
    # 检测链接
    # 对于 GitHub API，如果使用 token，可以更高并发
    # 未使用 token 时，降低并发以避免触发速率限制
    max_workers = 20 if github_token else 5
    rate_limit_delay = 0.05 if github_token else 0.5
    
    results = check_all_links(
        links,
        github_token,
        max_workers=max_workers,
        rate_limit_delay=rate_limit_delay
    )
    
    # 统计结果
    print()
    print("=" * 60)
    valid_count = sum(1 for r in results if r.is_valid)
    invalid_count = len(results) - valid_count
    print(f"检测完成: 有效 {valid_count}, 无效 {invalid_count}")
    
    # 如果需要，删除无效行
    if args.delete and invalid_count > 0:
        print()
        print("正在删除无效链接...")
        deleted = delete_invalid_lines(readme_path, results)
        print(f"已删除 {deleted} 行")
    
    # 返回退出码
    if invalid_count > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()