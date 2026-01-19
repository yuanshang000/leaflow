#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeafLow 网站自动签到脚本 - 青龙面板定制版 (v5)

功能:
- 通过环境变量 LEAFLOW_COOKIES 获取用户凭证进行自动签到。
- 支持多账号，环境变量中用 & 或 换行符 分隔。
- 自动从青龙面板配置文件读取并使用企业微信、Telegram等推送通知。
- 无需额外配置文件，单脚本即可运行。

更新日志 (v5):
- 根据用户提供的已签到页面HTML，重写了奖励提取逻辑。
- 新增针对性的 HTML 结构匹配，优先从 class="reward-amount" 的 div 中提取奖励，准确率更高。
- 保留旧的文本匹配作为备用方案，增强了脚本的兼容性和稳定性。
"""

import json
import time
import sys
import logging
import os
import requests
import re
from urllib.parse import unquote

# --- 通知服务 ---
try:
    from notify import send
except ImportError:
    def send(title, content):
        print("="*60)
        print(f"通知标题: {title}")
        print(f"通知内容:\n{content}")
        print("="*60)
        print("未找到青龙面板的 notify.py，通知仅打印在日志中。")

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class LeafLowCheckin:
    """
    LeafLow 签到主类
    """
    def __init__(self, cookies_list):
        self.cookies_list = cookies_list
        self.checkin_url = "https://checkin.leaflow.net"
        self.main_site = "https://leaflow.net"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        self.results = []

    @staticmethod
    def parse_cookie_string(cookie_string):
        cookies = {}
        for cookie in cookie_string.split(';'):
            cookie = cookie.strip()
            if '=' in cookie:
                name, value = cookie.split('=', 1)
                cookies[name.strip()] = unquote(value.strip())
        return cookies

    def create_session(self, cookies_dict):
        session = requests.Session()
        session.headers.update({
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
        })
        requests.utils.add_dict_to_cookiejar(session.cookies, cookies_dict)
        return session

    def test_authentication(self, session, account_name):
        test_urls = [
            f"{self.main_site}/dashboard",
            f"{self.main_site}/user",
            f"{self.main_site}/profile",
        ]
        try:
            for url in test_urls:
                logger.debug(f"[{account_name}] 正在尝试访问 {url} 进行认证测试...")
                response = session.get(url, timeout=30, allow_redirects=True)
                if response.status_code == 200 and any(kw in response.text.lower() for kw in ['dashboard', 'logout', 'profile', 'user']):
                    logger.info(f"✅ [{account_name}] Cookie 有效，通过访问 {url} 认证成功。")
                    return True, "认证成功"
                if 'login' in response.url.lower():
                    logger.warning(f"[{account_name}] 访问 {url} 被重定向到登录页，Cookie 可能已失效。")
                    return False, "认证失败，Cookie 已失效，被重定向到登录页。"
            return False, f"认证失败，尝试了 {len(test_urls)} 个页面均无法确认登录状态。"
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [{account_name}] 测试认证时发生网络错误: {e}")
            return False, f"测试认证时发生网络错误: {e}"

    def extract_reward(self, html_content):
        """
        从页面内容中提取奖励信息。优先使用HTML结构匹配，失败则使用文本匹配。
        Args:
            html_content (str): 页面HTML文本
        Returns:
            tuple: (amount, unit) or None if not found. e.g., ('0.07', '元')
        """
        # 方案一：精准HTML结构匹配 (优先级最高)
        # 匹配 <div class="reward-amount">...</div> 结构
        structure_pattern = re.compile(r'class="reward-amount"[^>]*>\s*([\d\.]+)\s*([^<\s]+)\s*<')
        match = structure_pattern.search(html_content)
        if match:
            amount = match.group(1)
            unit = match.group(2)
            logger.debug(f"通过HTML结构匹配成功: 金额={amount}, 单位={unit}")
            return amount, unit

        # 方案二：模糊文本匹配 (作为备用)
        text_patterns = [
            re.compile(r'(?:获得|奖励|领取了?)\s*(\d+\.?\d*)\s*([a-zA-Z\u4e00-\u9fa5]+)'),
            re.compile(r'earned\s*(\d+\.?\d*)\s*(credits?|points?)', re.IGNORECASE),
            re.compile(r'got\s*(\d+\.?\d*)\s*(credits?|points?)', re.IGNORECASE),
            re.compile(r'(\d+\.?\d*)\s*(?:points|credits|积分|硬币|元)', re.IGNORECASE)
        ]
        for pattern in text_patterns:
            match = pattern.search(html_content)
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    amount = groups[0]
                    unit = groups[1].strip("<>\"',. ")
                    if len(unit) < 10:
                        logger.debug(f"通过文本匹配成功: 金额={amount}, 单位={unit}")
                        return amount, unit
        
        logger.debug("所有奖励匹配方案均失败。")
        return None

    def perform_checkin(self, session, account_name):
        """
        执行签到操作，并无论如何都尝试提取奖励。
        """
        logger.info(f"🎯 [{account_name}] 正在访问签到页面...")
        try:
            response_get = session.get(self.checkin_url, timeout=30)
            if response_get.status_code != 200:
                return False, f"访问签到页失败，状态码: {response_get.status_code}"

            html_content = response_get.text
            reward_info = self.extract_reward(html_content)

            # 检查是否已经签到
            if any(indicator in html_content.lower() for indicator in ['already checked in', '今日已签到']):
                if reward_info:
                    amount, unit = reward_info
                    message = f"今天已经签到过了。今日奖励: {amount} {unit}。"
                else:
                    message = "今天已经签到过了。(未能从页面获取今日奖励信息)"
                logger.info(f"✅ [{account_name}] {message}")
                return True, message

            # 如果未签到，则执行签到动作 (POST)
            logger.info(f"[{account_name}] 尚未签到，正在执行签到操作...")
            response_post = session.post(self.checkin_url, data={'checkin': '1'}, timeout=30)

            if response_post.status_code == 200:
                post_html_content = response_post.text
                success_indicators = ['check-in successful', 'checkin successful', '签到成功', 'success', '已签到']

                if any(indicator in post_html_content.lower() for indicator in success_indicators):
                    reward_info_post = self.extract_reward(post_html_content)
                    if reward_info_post:
                        amount, unit = reward_info_post
                        message = f"签到成功！获得了 {amount} {unit}。"
                    else:
                        message = "签到成功！(未能从返回信息中提取具体奖励)"
                    
                    logger.info(f"✅ [{account_name}] {message}")
                    return True, message
                else:
                    return False, "签到请求已发送，但响应中未找到成功标识。"
            else:
                return False, f"签到 POST 请求失败，状态码: {response_post.status_code}"

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [{account_name}] 签到过程中发生网络错误: {e}")
            return False, f"签到过程中发生网络错误: {e}"
        except Exception as e:
            logger.error(f"❌ [{account_name}] 签到过程中发生未知错误: {e}")
            return False, f"签到过程中发生未知错误: {e}"
            
    def run(self):
        if not self.cookies_list or not self.cookies_list[0]:
            logger.error("❌ 未找到有效的 LEAFLOW_COOKIES 环境变量，请检查配置。")
            self.results.append({'account': 'N/A', 'success': False, 'message': '未配置Cookie'})
            return

        logger.info(f"💎 共找到 {len(self.cookies_list)} 个账号，即将开始签到...")
        
        for i, cookie_string in enumerate(self.cookies_list):
            account_name = f"账号{i + 1}"
            logger.info(f"\n" + "-"*30 + f" 正在处理 {account_name} " + "-"*30)
            
            cookies_dict = self.parse_cookie_string(cookie_string)
            if not cookies_dict:
                self.results.append({'account': account_name, 'success': False, 'message': 'Cookie格式错误'})
                continue
            
            session = self.create_session(cookies_dict)
            
            auth_success, auth_message = self.test_authentication(session, account_name)
            if not auth_success:
                self.results.append({'account': account_name, 'success': False, 'message': auth_message})
                continue

            checkin_success, checkin_message = self.perform_checkin(session, account_name)
            self.results.append({
                'account': account_name,
                'success': checkin_success,
                'message': checkin_message
            })

            if i < len(self.cookies_list) - 1:
                delay = 3
                logger.info(f"⏱️  等待 {delay} 秒后处理下一个账号...")
                time.sleep(delay)

    def generate_report(self):
        success_count = sum(1 for r in self.results if r['success'])
        total_count = len(self.results)
        
        title = f"LeafLow 签到报告 ({success_count}/{total_count})"
        
        content_lines = [f"签到任务完成，总计 {total_count} 个账号，成功 {success_count} 个。\n"]
        for result in self.results:
            status_icon = "✅" if result['success'] else "❌"
            line = f"{status_icon} {result['account']}: {result['message']}"
            content_lines.append(line)
            
        return title, "\n".join(content_lines)


def main():
    cookies_env = os.environ.get('LEAFLOW_COOKIES')
    
    if not cookies_env:
        logger.error("错误：环境变量 LEAFLOW_COOKIES 未设置！脚本无法运行。")
        send("LeafLow签到失败", "错误：未在青龙面板环境变量中找到 LEAFLOW_COOKIES，请添加后再试。")
        sys.exit(1)
        
    if '&' in cookies_env:
        cookies_list = [c.strip() for c in cookies_env.split('&')]
    elif '\n' in cookies_env:
        cookies_list = [c.strip() for c in cookies_env.split('\n')]
    else:
        cookies_list = [cookies_env.strip()]
        
    cookies_list = [c for c in cookies_list if c]

    checkin_task = LeafLowCheckin(cookies_list)
    checkin_task.run()
    
    title, content = checkin_task.generate_report()
    send(title, content)


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🚀 LeafLow 签到脚本启动")
    logger.info("=" * 60)
    main()
    logger.info("\n" + "=" * 60)
    logger.info("🏁 LeafLow 签到脚本执行完毕")
    logger.info("=" * 60)
