"""
文本处理工具函数。
"""
import re
import logging
import traceback
from typing import Dict, List, Tuple

try:
    from pyhtml2md import convert as html2md
    PYHTML2MD_AVAILABLE = True
    logging.info("pyhtml2md 可用")
except ImportError:
    PYHTML2MD_AVAILABLE = False
    logging.warning("pyhtml2md 不可用，请安装依赖: pip install pyhtml2md")

logger = logging.getLogger(__name__)

def html_to_plain_text(html_content: str) -> str:
    """
    将HTML内容直接转换为纯文本，只保留文本内容，过滤掉所有HTML元素和格式。
    
    Args:
        html_content: HTML内容
        
    Returns:
        纯文本内容
    """
    if not html_content:
        return ""
    
    try:
        # 移除可能导致问题的脚本、样式和其他非必要内容
        cleaned_html = html_content
        cleaned_html = re.sub(r'<style[\s\S]*?</style>', '', cleaned_html, flags=re.DOTALL)
        cleaned_html = re.sub(r'<script[\s\S]*?</script>', '', cleaned_html, flags=re.DOTALL)
        
        # 移除图片和嵌入内容
        cleaned_html = re.sub(r'<img[^>]*>', '', cleaned_html)
        cleaned_html = re.sub(r'<iframe[\s\S]*?</iframe>', '', cleaned_html, flags=re.DOTALL)
        cleaned_html = re.sub(r'<object[\s\S]*?</object>', '', cleaned_html, flags=re.DOTALL)
        cleaned_html = re.sub(r'<embed[\s\S]*?</embed>', '', cleaned_html, flags=re.DOTALL)
        cleaned_html = re.sub(r'<svg[\s\S]*?</svg>', '', cleaned_html, flags=re.DOTALL)
        cleaned_html = re.sub(r'<canvas[\s\S]*?</canvas>', '', cleaned_html, flags=re.DOTALL)
        
        # 处理表格，提取其文本内容
        tables = re.findall(r'<table[^>]*>[\s\S]*?</table>', cleaned_html, flags=re.IGNORECASE)
        for table in tables:
            # 提取所有表格单元格的文本，并用空格连接
            cells_text = re.findall(r'<t[dh][^>]*>([\s\S]*?)</t[dh]>', table, flags=re.IGNORECASE)
            cleaned_cells = []
            for cell in cells_text:
                # 清理单元格内的HTML标签
                clean_cell = re.sub(r'<[^>]+>', ' ', cell).strip()
                if clean_cell:
                    cleaned_cells.append(clean_cell)
            
            # 将单元格文本组合成一个字符串，替换原表格
            table_text = ' '.join(cleaned_cells)
            cleaned_html = cleaned_html.replace(table, table_text)
        
        # 使用更简单的方法处理常见HTML元素
        # 将换行相关标签转换为实际的换行
        cleaned_html = re.sub(r'<br\s*/?>|<p[^>]*>|</p>|<div[^>]*>|</div>|<li[^>]*>|<h[1-6][^>]*>', '\n', cleaned_html)
        
        # 去除所有剩余的HTML标签
        cleaned_html = re.sub(r'<[^>]+>', '', cleaned_html)
        
        # 处理HTML实体
        html_entities = {
            '&nbsp;': ' ', '&lt;': '<', '&gt;': '>', '&amp;': '&', 
            '&quot;': '"', '&apos;': "'", '&ndash;': '-', '&mdash;': '-',
            '&lsquo;': "'", '&rsquo;': "'", '&ldquo;': '"', '&rdquo;': '"'
        }
        for entity, replacement in html_entities.items():
            cleaned_html = cleaned_html.replace(entity, replacement)
        
        # 清理连续的空白字符
        cleaned_html = re.sub(r'\s+', ' ', cleaned_html)
        # 处理连续的换行
        cleaned_html = re.sub(r'\n{2,}', '\n\n', cleaned_html)
        # 移除行首和行尾的空白
        cleaned_html = re.sub(r'^\s+|\s+$', '', cleaned_html, flags=re.MULTILINE)
        
        logger.info(f"从HTML提取纯文本成功，长度: {len(cleaned_html)} 字符")
        return cleaned_html.strip()
        
    except Exception as e:
        logger.error(f"HTML转纯文本时出错: {e}")
        logger.error(traceback.format_exc())
        # 使用更简单的方法作为回退
        simple_text = re.sub(r'<[^>]*>', ' ', html_content)
        simple_text = re.sub(r'\s+', ' ', simple_text).strip()
        return simple_text

def html_to_markdown(html_content: str, simplified: bool = True, as_plain_text: bool = False) -> str:
    """
    将HTML内容转换为Markdown格式或纯文本。
    
    使用pyhtml2md库将HTML转换为Markdown，如果库不可用则使用备用方法。
    
    Args:
        html_content: HTML内容
        simplified: 是否使用简化模式，只保留标题和文本内容，过滤掉图片、表格等
        as_plain_text: 是否直接返回纯文本而不是Markdown格式
        
    Returns:
        Markdown格式的文本或纯文本
    """
    if not html_content:
        return ""
    
    # 如果要求返回纯文本，直接调用html_to_plain_text函数
    if as_plain_text:
        return html_to_plain_text(html_content)
    
    try:
        # 如果pyhtml2md可用，使用它进行转换
        if PYHTML2MD_AVAILABLE:
            # 清理HTML中的特殊问题（如果有）
            cleaned_html = html_content
            
            # 如果启用简化模式，预处理HTML内容
            if simplified:
                # 移除脚本和样式标签
                cleaned_html = re.sub(r'<style[\s\S]*?</style>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<script[\s\S]*?</script>', '', cleaned_html, flags=re.DOTALL)
                
                # 移除图片标签
                cleaned_html = re.sub(r'<img[^>]*>', '', cleaned_html)
                
                # 移除表格 (考虑到内容可能会丢失，但符合要求只保留文本)
                cleaned_html = re.sub(r'<table[\s\S]*?</table>', '', cleaned_html, flags=re.DOTALL)
                
                # 移除复杂的嵌入内容
                cleaned_html = re.sub(r'<iframe[\s\S]*?</iframe>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<object[\s\S]*?</object>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<embed[\s\S]*?</embed>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<svg[\s\S]*?</svg>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<canvas[\s\S]*?</canvas>', '', cleaned_html, flags=re.DOTALL)
                
                # 清理包含data属性的标签，常见于嵌入内容
                cleaned_html = re.sub(r'<[^>]*?data-[^>]*?>', '', cleaned_html)
                
                # 移除不必要的div类，如广告、社交媒体按钮等
                cleaned_html = re.sub(r'<div[^>]*?class=["\'](ad|banner|social|share|comment|footer)[^\'"]*?["\'][^>]*?>[\s\S]*?</div>', '', cleaned_html, flags=re.IGNORECASE | re.DOTALL)
            else:
                # 仅移除可能导致问题的脚本和样式标签
                cleaned_html = re.sub(r'<style[\s\S]*?</style>', '', cleaned_html, flags=re.DOTALL)
                cleaned_html = re.sub(r'<script[\s\S]*?</script>', '', cleaned_html, flags=re.DOTALL)
            
            # 转换为Markdown
            markdown_text = html2md(cleaned_html)
            
            # 移除多余的空行和空格
            markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
            markdown_text = re.sub(r'^\s+|\s+$', '', markdown_text, flags=re.MULTILINE)
            
            # 如果启用简化模式，移除Markdown中的图片和链接语法，只保留文本部分
            if simplified:
                # 移除图片语法 ![alt](url)
                markdown_text = re.sub(r'!\[.*?\]\(.*?\)', '', markdown_text)
                
                # 处理链接语法 [text](url)，只保留文本部分
                markdown_text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', markdown_text)
                
                # 移除多余的空行，再次清理
                markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
            
            logger.info(f"使用pyhtml2md成功将HTML转换为Markdown，长度: {len(markdown_text)} 字符")
            return markdown_text.strip()
        else:
            # 如果库不可用，使用现有的纯文本提取函数
            logger.info("pyhtml2md不可用，使用extract_text_from_html作为备选")
            return extract_text_from_html(html_content)
    except Exception as e:
        logger.error(f"HTML转Markdown时出错: {e}")
        logger.error(traceback.format_exc())
        # 出错时回退到现有的纯文本提取
        return extract_text_from_html(html_content)

def extract_text_from_html(html_content: str) -> str:
    """
    从HTML内容中提取纯文本，确保清理所有HTML标签，同时尽可能保留重要的文本结构。
    
    Args:
        html_content: HTML内容
        
    Returns:
        提取的纯文本
    """
    if not html_content:
        return ""
    
    try:
        # 移除HTML标签
        # 先删除可能包含大量内容的特定部分
        text = re.sub(r'<head[\s\S]*?</head>', '', html_content, flags=re.DOTALL)  # 移除head标签内容
        text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.DOTALL)  # 移除style标签内容
        text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.DOTALL)  # 移除script标签内容
        
        # 特殊处理表格：将表格转换为简单的文本表示
        # 查找所有表格
        tables = re.findall(r'<table[^>]*>[\s\S]*?</table>', text, flags=re.IGNORECASE)
        for table in tables:
            # 处理表头
            headers = re.findall(r'<th[^>]*>([\s\S]*?)</th>', table, flags=re.IGNORECASE)
            # 处理行
            rows = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', table, flags=re.IGNORECASE)
            
            # 构建简单的文本表格
            table_text = "\n\n表格:\n"
            if headers:
                header_line = " | ".join([re.sub(r'<[^>]*>', '', h).strip() for h in headers])
                table_text += f"{header_line}\n"
                table_text += "-" * len(header_line) + "\n"
            
            for row in rows:
                # 提取单元格内容
                cells = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row, flags=re.IGNORECASE)
                if cells:
                    # 清理单元格内容中的HTML
                    cell_content = [re.sub(r'<[^>]*>', '', cell).strip() for cell in cells]
                    table_text += " | ".join(cell_content) + "\n"
            
            # 替换原始表格
            text = text.replace(table, table_text)
        
        # 特殊处理有序列表
        ol_lists = re.findall(r'<ol[^>]*>([\s\S]*?)</ol>', text, flags=re.IGNORECASE)
        for ol in ol_lists:
            # 提取列表项
            items = re.findall(r'<li[^>]*>([\s\S]*?)</li>', ol, flags=re.IGNORECASE)
            list_text = "\n"
            for i, item in enumerate(items, 1):
                # 清理列表项内容中的HTML
                clean_item = re.sub(r'<[^>]*>', '', item).strip()
                list_text += f"{i}. {clean_item}\n"
            # 替换原始列表
            text = text.replace(ol, list_text)
        
        # 特殊处理无序列表
        ul_lists = re.findall(r'<ul[^>]*>([\s\S]*?)</ul>', text, flags=re.IGNORECASE)
        for ul in ul_lists:
            # 提取列表项
            items = re.findall(r'<li[^>]*>([\s\S]*?)</li>', ul, flags=re.IGNORECASE)
            list_text = "\n"
            for item in items:
                # 清理列表项内容中的HTML
                clean_item = re.sub(r'<[^>]*>', '', item).strip()
                list_text += f"• {clean_item}\n"
            # 替换原始列表
            text = text.replace(ul, list_text)
        
        # 处理一些常见的内容替换
        # 替换<br>, <p>, <div>等标签为换行
        text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<p[^>]*>', '\n\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</p>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<div[^>]*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<h[1-6][^>]*>', '\n\n', text, flags=re.IGNORECASE)  # 标题开始
        text = re.sub(r'</h[1-6]>', '\n', text, flags=re.IGNORECASE)  # 标题结束
        
        # 特殊处理引用块
        text = re.sub(r'<blockquote[^>]*>', '\n\n> ', text, flags=re.IGNORECASE)
        text = re.sub(r'</blockquote>', '\n\n', text, flags=re.IGNORECASE)
        
        # 特殊处理代码块
        text = re.sub(r'<pre[^>]*>|<code[^>]*>', '\n```\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</pre>|</code>', '\n```\n', text, flags=re.IGNORECASE)
        
        # 替换常见的HTML实体
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&amp;', '&')
        text = text.replace('&quot;', '"')
        text = text.replace('&apos;', "'")
        text = text.replace('&ndash;', '–')
        text = text.replace('&mdash;', '—')
        text = text.replace('&lsquo;', ''')
        text = text.replace('&rsquo;', ''')
        text = text.replace('&ldquo;', '"')
        text = text.replace('&rdquo;', '"')
        
        # 使用更强力的HTML标签移除
        # 这会移除所有剩余的HTML标签
        text = re.sub(r'<[^>]+>', '', text)
        
        # 二次检查：确保没有任何<>标签形式的内容残留
        # 这会捕获不规范的HTML标签
        text = re.sub(r'<[^<>]*>', '', text)
        
        # 处理连续的空白字符
        text = re.sub(r' {2,}', ' ', text)
        
        # 处理连续的换行，但保留段落结构
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # 移除行首和行尾的空白
        text = re.sub(r'^\s+|\s+$', '', text, flags=re.MULTILINE)
        
        return text.strip()
    except Exception as e:
        # 如果提取失败，尝试使用更简单的方法
        logger.error(f"提取HTML文本时出错: {e}")
        logger.error(traceback.format_exc())
        # 使用简单但可靠的标签移除
        simple_text = re.sub(r'<[^<>]*>', '', html_content)
        return simple_text.strip()

def extract_important_links(html_content: str) -> List[Dict[str, str]]:
    """
    从HTML内容中提取重要链接，特别是按钮和功能性链接。
    增强对"click here to unsubscribe"这类情况的支持。
    
    Args:
        html_content: HTML内容
        
    Returns:
        重要链接列表，每个链接是包含text和url的字典
    """
    if not html_content:
        return []
    
    important_links = []
    
    try:
        # 常见按钮和链接文本模式（中英文）
        button_patterns = [
            # 中文常见按钮文本
            r'查看详情', r'点击查看', r'了解更多', r'查看全文', r'立即查看', 
            r'立即购买', r'马上抢购', r'点击购买', r'立即注册', r'点击注册',
            r'确认', r'确定', r'同意', r'接受', r'取消订阅', r'退订', 
            r'取消', r'下载', r'申请', r'领取', r'激活', r'验证',
            
            # 英文常见按钮文本
            r'View', r'Click here', r'Learn more', r'Read more', r'See details',
            r'Buy now', r'Purchase', r'Register', r'Sign up', r'Subscribe',
            r'Confirm', r'Accept', r'Unsubscribe', r'Cancel', r'Download',
            r'Apply', r'Claim', r'Activate', r'Verify', r'Get started',
        ]
        
        # 链接上下文关键词（通常出现在链接附近）
        context_keywords = [
            # 英文
            'unsubscribe', 'subscribe', 'verify', 'confirm', 'activate', 
            'view', 'download', 'register', 'sign up', 'login', 'log in',
            'reset', 'password', 'account', 'profile', 'preferences',
            # 中文
            '退订', '订阅', '验证', '确认', '激活', '查看', '下载', '注册', 
            '登录', '重置', '密码', '账户', '账号', '个人资料', '偏好设置'
        ]
        
        # 特别处理的短语模式 - 专门针对"click here to unsubscribe"这类情况
        special_phrases = [
            (r'click\s+here\s+(?:to|and)\s+(unsubscribe|subscribe|verify|confirm|register)', 
             r'click here to \1'),  # "click here to unsubscribe"
            (r'(?:please\s+)?(click|tap|press)\s+(?:here|below|this)\s+(?:to|and)\s+(.*?)(?:[\.,]|$)', 
             r'\1 here to \2'),  # "please click here to reset your password"
            (r'(?:please\s+)?(点击|点选|按下)(?:这里|此处)\s*(?:以|来)?\s*(.*?)(?:[\.,]|$)', 
             r'\1\2'),  # "请点击这里以重置密码"
        ]
        
        # 更健壮的链接提取正则表达式，支持不同的引号格式和空格
        a_tags_patterns = [
            # 标准格式：href="url"
            r'<a\s+[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>',
            # 处理无引号或引号不匹配的情况
            r'<a\s+[^>]*href=([^\s>\"\']+)[^>]*>(.*?)</a>',
            # 处理可能的空格或转义字符
            r'<a\s+[^>]*href\s*=\s*[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>'
        ]
        
        # 合并所有匹配到的链接
        a_tags = []
        for pattern in a_tags_patterns:
            a_tags.extend(re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE))
        
        # 去除重复的链接
        processed_urls = set()
        
        for match in a_tags:
            # 兼容不同匹配结果的格式
            if isinstance(match, tuple) and len(match) >= 2:
                url, link_text = match[0], match[1]
            else:
                continue  # 跳过无效匹配
                
            # 跳过已处理的URL
            if url in processed_urls:
                continue
                
            # 清理URL（移除前后空格和引号）
            url = url.strip('\'" \t')
            
            # 清理链接文本中的HTML标签
            clean_text = re.sub(r'<[^>]+>', ' ', link_text).strip()
            clean_text = re.sub(r'\s+', ' ', clean_text)
            
            # 如果链接文本为空，跳过
            if not clean_text:
                continue
                
            # 检查链接文本是否包含任何重要按钮文本模式
            is_important = False
            for pattern in button_patterns:
                if re.search(pattern, clean_text, re.IGNORECASE):
                    is_important = True
                    break
            
            # 检查链接是否包含常见的功能性URL特征
            url_lower = url.lower()
            if any(keyword in url_lower for keyword in ['unsubscribe', 'view', 'confirm', 'verify', 'download', 'login', 'register', 'signup', 'subscribe', 'cancel', 'auth', 'activate']):
                is_important = True
                
            # 如果是中文URL特征
            if any(keyword in url_lower for keyword in ['退订', '查看', '确认', '验证', '下载', '登录', '注册', '订阅', '取消', '认证', '激活']):
                is_important = True
            
            # 检查是否是单按钮（文本长度小于15个字符）
            if len(clean_text) < 15 and re.search(r'\b(click|tap|view|see|read|check|buy|get|sign|subscribe|download|unsubscribe|退订|查看|点击|购买|注册|下载)\b', clean_text, re.IGNORECASE):
                is_important = True
            
            # 提取链接的上下文和段落
            context_text = ""
            if is_important or clean_text.lower() in ["click here", "here", "点击这里", "这里"]:
                # 查找链接所在的段落
                # 找到包含该链接的<p>标签
                p_pattern = r'<p[^>]*>(?:(?!<p|</p>).)*?' + re.escape(link_text) + r'(?:(?!<p|</p>).)*?</p>'
                p_match = re.search(p_pattern, html_content, re.DOTALL | re.IGNORECASE)
                
                if p_match:
                    context_text = p_match.group(0)
                else:
                    # 如果找不到完整段落，尝试查找包含该链接的句子
                    # 找到包含该链接的整个句子（从上一个句号到下一个句号）
                    sent_pattern = r'[.!?][ \t\n]*(?:[^.!?]*?' + re.escape(link_text) + r'[^.!?]*?[.!?])'
                    sent_match = re.search(sent_pattern, html_content, re.DOTALL | re.IGNORECASE)
                    
                    if sent_match:
                        context_text = sent_match.group(0).lstrip('.!? \t\n')
                    else:
                        # 找不到完整句子，使用链接前后的文本作为上下文
                        try:
                            link_index = html_content.find(link_text)
                            if link_index > -1:
                                start = max(0, link_index - 150)
                                end = min(len(html_content), link_index + len(link_text) + 150)
                                context_text = html_content[start:end]
                        except:
                            context_text = ""
                
                if context_text:
                    # 清理上下文文本中的HTML标签
                    clean_context = re.sub(r'<[^>]+>', ' ', context_text).strip()
                    clean_context = re.sub(r'\s+', ' ', clean_context)
                    
                    # 特别处理的情况：针对"click here to unsubscribe"这类情况
                    if clean_text.lower() in ["click here", "here", "点击这里", "这里"]:
                        # 尝试识别特殊短语模式
                        for pattern, replacement in special_phrases:
                            context_match = re.search(pattern, clean_context, re.IGNORECASE)
                            if context_match:
                                # 提取完整短语作为显示文本
                                full_phrase = context_match.group(0)
                                clean_text = full_phrase
                                is_important = True
                                break
                    
                    # 检查上下文是否包含重要关键词
                    if not is_important:
                        for keyword in context_keywords:
                            if keyword.lower() in clean_context.lower():
                                is_important = True
                                # 使用上下文关键句作为链接文本
                                # 尝试提取包含关键词的句子
                                keyword_pattern = r'[^.!?]*' + re.escape(keyword) + r'[^.!?]*[.!?]?'
                                keyword_match = re.search(keyword_pattern, clean_context, re.IGNORECASE)
                                if keyword_match:
                                    clean_text = keyword_match.group(0).strip()
                                else:
                                    clean_text = clean_context
                                break
            
            # 如果是重要链接，添加到列表
            if is_important:
                # 添加协议前缀，如果URL是相对URL
                if not url.startswith(('http://', 'https://', 'mailto:')):
                    # 如果URL以//开头（协议相对URL）
                    if url.startswith('//'):
                        url = 'https:' + url
                    # 如果URL以/开头（网站根路径）
                    elif url.startswith('/'):
                        # 尝试从HTML内容中推断基本域名
                        base_match = re.search(r'<base\s+href=["\']([^"\']+)["\']', html_content)
                        if base_match:
                            base_url = base_match.group(1)
                            url = base_url.rstrip('/') + '/' + url.lstrip('/')
                        else:
                            # 无法确定基础URL，保持原样
                            pass
                    # 其他情况可能是相对于当前页面的路径
                    else:
                        # 由于无法确定当前页面URL，保持原样
                        pass
                
                # 记录已处理的URL
                processed_urls.add(url)
                
                # 避免重复添加相同链接
                if not any(link['url'] == url for link in important_links):
                    # 如果链接文本太长，可能是包含了太多上下文，进行截断
                    display_text = clean_text
                    if len(display_text) > 60:  # 限制显示文本长度
                        # 尝试找到关键短语
                        for keyword in context_keywords:
                            if keyword.lower() in clean_text.lower():
                                # 找到关键词前后的一小段文本
                                keyword_index = clean_text.lower().find(keyword.lower())
                                start = max(0, keyword_index - 20)
                                end = min(len(clean_text), keyword_index + len(keyword) + 20)
                                display_text = clean_text[start:end]
                                if start > 0:
                                    display_text = "..." + display_text
                                if end < len(clean_text):
                                    display_text = display_text + "..."
                                break
                    
                    important_links.append({
                        'text': display_text,
                        'url': url
                    })
        
        logger.info(f"从HTML提取了 {len(important_links)} 个重要链接")
        return important_links
    
    except Exception as e:
        logger.error(f"提取重要链接时出错: {e}")
        logger.error(traceback.format_exc())
        return []

def extract_meaningful_summary(text: str, max_length: int) -> str:
    """
    提取文本的有意义摘要，优先保留开头和重要句子。
    
    Args:
        text: 原始文本
        max_length: 最大长度限制
        
    Returns:
        摘要文本
    """
    if not text:
        return ""
    
    # 如果文本本身就很短，直接返回
    if len(text) <= max_length:
        return text
    
    # 清理文本，去除多余空白行和空格
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    text = text.strip()
    
    # 尝试过滤掉常见的邮件引用格式
    # 尝试识别并移除"On ... wrote:"格式
    quote_patterns = [
        r'On\s+.*wrote:[\s\S]*',  # 英文引用
        r'在[\s\S]*?写道:[\s\S]*',  # 中文引用
        r'From:[\s\S]*?Sent:[\s\S]*?To:[\s\S]*?Subject:[\s\S]*?',  # Outlook格式
        r'>.*\n(>.*\n)+',  # 引用行（以>开头）
        r'_{3,}.*_{3,}',  # 分隔线
        r'-{3,}.*-{3,}',  # 分隔线
    ]
    
    original_text = text
    for pattern in quote_patterns:
        text = re.sub(pattern, '', text, flags=re.MULTILINE)
    
    # 如果过滤后文本为空，回退到原始文本
    if not text.strip():
        text = original_text
    
    # 尝试找到第一段完整文本
    paragraphs = text.split('\n\n')
    first_paragraph = paragraphs[0] if paragraphs else ""
    
    # 如果第一段足够短，直接使用
    if len(first_paragraph) <= max_length:
        return first_paragraph
    
    # 如果第一段还是太长，尝试找到有意义的句子
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    # 构建摘要，优先使用开头句子
    summary = ""
    for sentence in sentences:
        # 如果添加这个句子会超出限制，结束循环
        if len(summary) + len(sentence) + 5 > max_length:
            break
        
        # 添加句子到摘要
        if summary:
            summary += " " + sentence
        else:
            summary = sentence
    
    # 如果摘要为空（可能是一个很长的句子），截取开头部分
    if not summary:
        summary = text[:max_length-3].strip() + "..."
    # 如果摘要比原文短，添加省略号
    elif len(summary) < len(text):
        summary += "..."
    
    return summary.strip()

def extract_email_content_with_links(html_content: str, max_length: int = 1000) -> Tuple[str, List[Dict[str, str]]]:
    """
    从HTML邮件中提取有意义的内容摘要和重要链接。
    
    Args:
        html_content: HTML邮件内容
        max_length: 文本摘要的最大长度限制
        
    Returns:
        提取的纯文本摘要和重要链接列表的元组
    """
    # 提取纯文本内容
    plain_text = html_to_plain_text(html_content) if html_content else ""
    
    # 提取重要链接
    important_links = extract_important_links(html_content)
    
    # 提取文本摘要
    text_summary = extract_meaningful_summary(plain_text, max_length)
    
    return text_summary, important_links

# 测试函数，用于验证链接提取功能
def test_extract_important_links():
    """
    测试链接提取功能，包括基本链接和上下文相关链接。
    
    Returns:
        测试结果字典，包含提取的链接
    """
    # 创建测试HTML，包含各种链接样式
    test_html = """
    <html>
    <body>
        <p>Welcome to our newsletter!</p>
        
        <!-- 普通按钮链接 -->
        <p>Please <a href="https://example.com/confirm">Confirm your subscription</a> to continue.</p>
        
        <!-- "click here" + 上下文 类型链接 -->
        <p>If you no longer wish to receive these emails, <a href="https://example.com/unsubscribe">click here</a> to unsubscribe.</p>
        
        <!-- 中文退订链接 -->
        <p>如果您不想再收到此类邮件，请<a href="https://example.cn/unsubscribe">点击这里</a>退订。</p>
        
        <!-- 带图标的链接 -->
        <p><a href="https://example.com/download"><img src="download.png"> Download your document</a></p>
        
        <!-- 普通非重要链接 -->
        <p>Visit our <a href="https://example.com">website</a> for more information.</p>
        
        <!-- URL中包含关键词的链接 -->
        <p><a href="https://example.com/reset-password">Reset your password</a></p>
    </body>
    </html>
    """
    
    # 提取链接
    links = extract_important_links(test_html)
    
    # 返回测试结果
    return {
        "extracted_links": links,
        "total_links": len(links)
    } 