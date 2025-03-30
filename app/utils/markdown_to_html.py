"""
将Markdown文本转换为HTML的工具模块。
用于邮件正文的格式转换。
"""
import re
import logging
import html

# 配置日志
logger = logging.getLogger(__name__)

def convert_markdown_to_html(text, use_advanced=True):
    """
    将Markdown格式的文本转换为HTML格式
    
    Args:
        text (str): Markdown格式的文本
        use_advanced (bool): 是否使用高级Markdown转换（需要markdown扩展包）
        
    Returns:
        str: 转换后的HTML文本，包含完整的样式
    """
    if not text:
        return ""
    
    # 预处理Markdown内容，确保加粗和代码块正确处理
    processed_text = preprocess_markdown(text)
    
    if use_advanced:
        try:
            # 尝试使用高级Markdown转换（需要额外的依赖包）
            return convert_advanced_markdown(processed_text)
        except ImportError as e:
            # 如果缺少依赖包，记录错误并回退到基本转换
            logger.warning(f"高级Markdown转换失败: {e}，回退到基本转换")
    
    # 使用基本的Markdown转换
    return convert_basic_markdown(processed_text)

def preprocess_markdown(text):
    """
    预处理Markdown文本，处理可能被转义的Markdown标记
    
    Args:
        text (str): 原始Markdown文本
        
    Returns:
        str: 预处理后的Markdown文本
    """
    # 处理转义的加粗标记 \*\* -> **
    text = re.sub(r'\\\*\\\*', '**', text)
    # 处理转义的内联代码标记 \` -> `
    text = re.sub(r'\\`', '`', text)
    # 处理转义的代码块标记 \`\`\` -> ```
    text = re.sub(r'\\`\\`\\`', '```', text)
    
    return text

def convert_advanced_markdown(text):
    """
    使用高级Markdown库和扩展来转换Markdown为HTML
    
    Args:
        text (str): 预处理后的Markdown文本
        
    Returns:
        str: 转换后的HTML，包含完整样式
    """
    import markdown
    from markdown.extensions.extra import ExtraExtension
    from markdown.extensions.nl2br import Nl2BrExtension
    from markdown.extensions.sane_lists import SaneListExtension
    from markdown.extensions.fenced_code import FencedCodeExtension
    from markdown.extensions.codehilite import CodeHiliteExtension
    
    # 使用markdown库进行高级转换
    html_body = markdown.markdown(
        text,
        extensions=[
            ExtraExtension(),          # 支持表格、脚注等
            Nl2BrExtension(),          # 将换行转换为<br>标签
            SaneListExtension(),        # 更好地处理列表
            FencedCodeExtension(),      # 支持代码块
            CodeHiliteExtension(       # 代码高亮
                noclasses=True,         # 使用行内样式而非CSS类
                pygments_style='default' # 使用默认高亮样式
            )
        ]
    )
    
    # 添加自定义CSS样式使邮件更美观
    styled_html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            h1, h2, h3 {{ color: #2c3e50; }}
            a {{ color: #3498db; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            pre {{ background-color: #f8f8f8; padding: 10px; border-radius: 5px; margin: 15px 0; overflow: auto; }}
            code {{ background-color: #f8f8f8; padding: 2px 4px; border-radius: 3px; font-family: Consolas, Monaco, 'Andale Mono', monospace; }}
            strong {{ font-weight: bold; color: #000; }}
            blockquote {{ border-left: 4px solid #ccc; padding-left: 16px; margin-left: 0; color: #777; }}
            ul, ol {{ padding-left: 20px; }}
            table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
        </style>
    </head>
    <body>
        {html_body}
    </body>
    </html>
    """
    
    logger.debug(f"高级转换 - 最终HTML内容前500字符: {styled_html[:500]}")
    return styled_html

def convert_basic_markdown(text):
    """
    使用基本的正则表达式替换将Markdown转换为HTML
    
    支持的格式：
    - *斜体* -> <em>斜体</em>
    - **粗体** -> <strong>粗体</strong>
    - __下划线__ -> <u>下划线</u>
    - ~~删除线~~ -> <del>删除线</del>
    - `代码` -> <code>代码</code>
    - ```代码块``` -> <pre><code>代码块</code></pre>
    - [链接文本](链接URL) -> <a href="链接URL">链接文本</a>
    
    Args:
        text (str): Markdown格式的文本
        
    Returns:
        str: 转换后的HTML文本
    """
    # 首先对HTML特殊字符进行转义，避免HTML注入
    escaped_text = html.escape(text)
    
    # 记录原始文本和转义后的文本，用于调试
    logger.debug(f"原始文本: {text}")
    logger.debug(f"转义后的文本: {escaped_text}")
    
    # 使用正则表达式进行替换
    
    # 替换链接 [文本](URL)
    html_text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', escaped_text)
    
    # 替换代码块 ```代码块```
    html_text = re.sub(r'```(.*?)```', r'<pre><code>\1</code></pre>', html_text, flags=re.DOTALL)
    
    # 替换行内代码 `代码`
    html_text = re.sub(r'`([^`]+)`', r'<code>\1</code>', html_text)
    
    # 替换粗体 **文本**
    html_text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', html_text)
    
    # 替换斜体 *文本*
    html_text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', html_text)
    
    # 替换下划线 __文本__
    html_text = re.sub(r'__([^_]+)__', r'<u>\1</u>', html_text)
    
    # 替换删除线 ~~文本~~
    html_text = re.sub(r'~~([^~]+)~~', r'<del>\1</del>', html_text)
    
    # 处理标题 (## 标题)
    html_text = re.sub(r'^#{1,6}\s+(.+)$', r'<h\1>\2</h\1>', html_text, flags=re.MULTILINE)
    
    # 处理无序列表 (- 项目)
    html_text = re.sub(r'^\s*[-*+]\s+(.+)$', r'<li>\1</li>', html_text, flags=re.MULTILINE)
    html_text = re.sub(r'(<li>.*?</li>)+', r'<ul>\g<0></ul>', html_text, flags=re.DOTALL)
    
    # 替换换行符为<br>
    html_text = html_text.replace('\n', '<br>')
    
    # 添加基本样式
    styled_html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
            blockquote {{ border-left: 4px solid #ccc; padding-left: 1em; color: #555; }}
            code {{ background-color: #f0f0f0; padding: 2px 4px; border-radius: 3px; }}
            pre {{ background-color: #f0f0f0; padding: 10px; border-radius: 3px; overflow-x: auto; }}
            a {{ color: #3498db; text-decoration: none; }}
            ul, ol {{ padding-left: 20px; }}
        </style>
    </head>
    <body>
        {html_text}
    </body>
    </html>
    """
    
    # 记录转换后的HTML文本，用于调试
    logger.debug(f"基本转换 - 最终HTML内容前500字符: {styled_html[:500]}")
    
    return styled_html 