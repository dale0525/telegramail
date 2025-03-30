"""
文本处理工具函数。
"""
import re
import logging
import traceback

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