"""
用于将HTML内容转换为图片的工具函数。
"""
import io
import base64
import re
import logging
import traceback
import asyncio
from typing import Optional, Tuple, Dict, Any
from PIL import Image
from datetime import datetime

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
    logging.info("playwright 可用")
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logging.warning("playwright 不可用，请安装依赖: pip install playwright")

# 导入HTML文本处理函数
from app.utils.text_utils import extract_text_from_html, html_to_markdown

logger = logging.getLogger(__name__)

def process_inline_images(html_content: str, inline_images: Dict[str, Any]) -> str:
    """
    处理HTML内容中的内联图片，将src="cid:xxx"替换为实际的Data URI
    
    Args:
        html_content: 原始HTML内容
        inline_images: 内联图片字典，键为CID，值为包含图片数据的字典
        
    Returns:
        处理后的HTML内容
    """
    if not inline_images or not html_content:
        return html_content
    
    logger.info(f"开始处理内联图片，共有 {len(inline_images)} 个")
    
    # 查找所有src="cid:xxx"模式
    def replace_cid(match):
        cid = match.group(1)
        if cid in inline_images:
            # 获取图片数据和MIME类型
            image_data = inline_images[cid]['data']
            content_type = inline_images[cid]['content_type']
            
            # 将图片数据转换为base64编码
            b64_data = base64.b64encode(image_data).decode('ascii')
            
            # 构建data URI
            data_uri = f"data:{content_type};base64,{b64_data}"
            logger.info(f"替换cid:{cid}为data URI")
            return f'src="{data_uri}"'
        else:
            logger.warning(f"未找到内联图片 cid:{cid}")
            return match.group(0)  # 保持原样
    
    # 查找并替换cid引用
    pattern = r'src="cid:([^"]+)"'
    processed_html = re.sub(pattern, replace_cid, html_content)
    
    # 查找并替换单引号版本
    pattern = r"src='cid:([^']+)'"
    processed_html = re.sub(pattern, replace_cid, processed_html)
    
    return processed_html

async def html_to_image(html_content: str) -> Optional[bytes]:
    """
    将HTML内容转换为图片。
    
    使用 Playwright 将 HTML 转换为图片。
    如果 Playwright 不可用，返回 None。
    
    Args:
        html_content: HTML内容
        
    Returns:
        图片的二进制数据，如果转换失败则返回None
    """
    # 确保我们有一些内容来渲染
    if not html_content or len(html_content.strip()) < 10:
        logger.warning("HTML内容太短或为空，无法转换为图片")
        return None
    
    logger.info(f"开始将HTML转换为图片，内容长度: {len(html_content)} 字符")
    
    # 不再预处理HTML内容，允许加载外部资源
    # html_content = _preprocess_html_for_rendering(html_content)
    
    # 添加基本样式以改善渲染
    styled_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            /* 最小化必要的样式设置 */
            html, body {{
                margin: 0;
                padding: 0;
                background: white;
            }}
            .content-wrapper {{
                padding: 10px;
                background: white;
                width: 100%;
            }}
            /* 图片占位符样式 */
            .img-placeholder {{
                display: inline-block;
                background-color: #f0f0f0;
                border: 1px dashed #ccc;
                text-align: center;
                padding: 10px;
                margin: 5px;
                color: #666;
                font-style: italic;
                font-size: 12px;
            }}
            /* 确保加载中的图片有占位符显示 */
            img {{
                min-height: 30px;
                min-width: 30px;
            }}
        </style>
    </head>
    <body>
        <div class="content-wrapper">
            {html_content}
        </div>
    </body>
    </html>
    """
    
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright不可用，无法渲染HTML")
        return None
    
    logger.info("使用Playwright渲染HTML")
    try:
        # 设置全局超时时间为15秒，避免整个操作长时间卡住
        async with async_playwright() as p:
            # 使用无头模式启动浏览器，设置超时时间
            browser = await p.chromium.launch(headless=True, timeout=30000)
            
            # 创建新页面，使用更大的初始视口确保长内容可被完整捕获
            page = await browser.new_page(
                viewport={'width': 800, 'height': 1200},
                device_scale_factor=2
            )
            
            # 设置页面超时参数
            page.set_default_timeout(15000)  # 设置页面操作默认超时为15秒
            page.set_default_navigation_timeout(10000)  # 导航超时设置为10秒
            
            # 不阻止外部资源加载
            
            # 开始计时
            start_time = datetime.now()
            
            # 设置页面内容
            try:
                await page.set_content(styled_html, timeout=10000)  # 设置内容加载超时时间为10秒
                logger.info("页面内容已设置")
            except Exception as e:
                logger.error(f"设置页面内容时超时: {e}")
                await browser.close()
                return None
            
            # 等待页面DOM加载完成
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)  # 先等待DOM加载完成
                logger.info("页面DOM加载完成")
            except Exception as e:
                logger.warning(f"等待页面DOM加载超时，继续执行: {e}")
                # 继续执行，不要终止整个流程
            
            # 尝试等待网络空闲状态，最多等待10秒
            try:
                logger.info("等待网络空闲状态，最多10秒...")
                await page.wait_for_load_state("networkidle", timeout=10000)
                logger.info("网络已空闲，页面资源加载完成")
            except Exception as e:
                logger.warning(f"等待网络空闲状态超时，将继续执行: {e}")
                # 标记未加载完成的图片，提供视觉反馈
                try:
                    await page.evaluate("""() => {
                        document.querySelectorAll('img').forEach(img => {
                            if (!img.complete) {
                                img.style.border = '1px dashed #ccc';
                                img.style.background = '#f0f0f0';
                                if (!img.alt) img.alt = '图片加载中断';
                            }
                        });
                        console.log('已标记未加载完成的图片');
                    }""")
                    logger.info("已标记未加载完成的图片")
                except Exception as img_error:
                    logger.warning(f"标记未加载完成的图片时出错: {img_error}")
            
            logger.info(f"总页面加载时间: {(datetime.now() - start_time).total_seconds():.2f} 秒")
            
            # 获取内容的实际高度以确保捕获完整内容
            try:
                content_height = await page.evaluate("""() => {
                    const wrapper = document.querySelector('.content-wrapper');
                    return wrapper ? wrapper.scrollHeight : 1200;
                }""")
                logger.info(f"内容实际高度: {content_height}px")
            except Exception as e:
                logger.warning(f"获取内容高度失败，使用默认高度: {e}")
                content_height = 1200
            
            # 确保视口高度足够显示全部内容
            if content_height > 1200:
                try:
                    await page.set_viewport_size({"width": 800, "height": content_height + 50})
                    logger.info(f"调整视口高度为: {content_height + 50}px")
                except Exception as e:
                    logger.warning(f"调整视口大小失败: {e}")
            
            # 精确调整尺寸以适应内容
            try:
                content_rect = await page.evaluate("""() => {
                    const wrapper = document.querySelector('.content-wrapper');
                    if (!wrapper) return { x: 0, y: 0, width: 800, height: 1200 };
                    
                    const rect = wrapper.getBoundingClientRect();
                    return {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: Math.max(rect.height, wrapper.scrollHeight)
                    };
                }""")
                
                logger.info(f"内容区域: x={content_rect['x']}, y={content_rect['y']}, width={content_rect['width']}, height={content_rect['height']}")
            except Exception as e:
                logger.warning(f"获取内容区域失败，使用默认值: {e}")
                content_rect = {'x': 0, 'y': 0, 'width': 800, 'height': content_height}
            
            # 截图操作也设置超时
            try:
                # 使用全页面模式确保长内容不被截断
                if content_rect['height'] > 5000:
                    logger.info("内容过长，使用全页面截图")
                    screenshot = await page.screenshot(
                        full_page=True,
                        type="jpeg",
                        quality=90,  # 稍微降低质量以减少处理时间
                        scale="device",
                        animations="disabled",
                        timeout=10000  # 减少截图超时时间
                    )
                else:
                    # 使用精确区域截图
                    screenshot = await page.screenshot(
                        clip={
                            'x': content_rect['x'],
                            'y': content_rect['y'],
                            'width': content_rect['width'],
                            'height': content_rect['height']
                        },
                        type="jpeg",
                        quality=90,
                        scale="device",
                        animations="disabled",
                        timeout=10000
                    )
                
                logger.info(f"截图完成，大小: {len(screenshot)} 字节，总耗时: {(datetime.now() - start_time).total_seconds():.2f} 秒")
                
                await browser.close()
                return screenshot
            except Exception as e:
                logger.error(f"截图操作失败: {e}")
                await browser.close()
                return None
            
    except Exception as e:
        logger.error(f"使用Playwright转换HTML为图片时出错: {e}")
        logger.error(f"错误详情: {traceback.format_exc()}")
        return None

async def html_to_document(html_content: str, subject: str = "邮件预览", plain_text: str = None, inline_images: Dict[str, Any] = None) -> Optional[Tuple[bytes, str]]:
    """
    将HTML内容转换为文档图片。
    
    Args:
        html_content: HTML内容
        subject: 邮件主题，用于生成文件名
        plain_text: 如果HTML内容为空，使用纯文本内容来生成预览
        inline_images: 内联图片字典，用于替换HTML中的cid引用
        
    Returns:
        (图片的二进制数据, 文件名)，如果转换失败则返回None
    """
    logger.info(f"开始将HTML转换为文档，主题: '{subject}'")
    logger.info(f"内容数据：HTML长度={len(html_content) if html_content else 0}, 纯文本长度={len(plain_text) if plain_text else 0}")
    
    # 简化HTML内容，过大的HTML会导致处理缓慢
    if html_content and len(html_content) > 500000:
        logger.warning(f"HTML内容过大（{len(html_content)}字节），将被截断")
        # 截取前500KB内容并添加提示
        html_content = html_content[:500000] + "\n<div style='text-align:center;padding:20px;'><b>邮件内容过大，仅显示部分内容</b></div>"
    
    # 如果提供了内联图片，首先处理替换HTML中的CID引用
    if inline_images and html_content:
        try:
            html_content = process_inline_images(html_content, inline_images)
            logger.info("已处理HTML中的内联图片引用")
        except Exception as e:
            logger.error(f"处理内联图片时出错: {e}")
            # 继续处理，即使内联图片处理失败
    
    # 如果HTML内容为空但有纯文本，创建一个简单的HTML来显示文本
    if (not html_content or len(html_content.strip()) < 10) and plain_text:
        logger.info("HTML内容为空或过短，使用纯文本内容创建预览")
        
        # 如果纯文本过大，截断它
        if plain_text and len(plain_text) > 50000:
            logger.warning(f"纯文本内容过大（{len(plain_text)}字节），将被截断")
            plain_text = plain_text[:50000] + "\n\n[邮件内容过大，仅显示部分内容]"
        
        # 如果纯文本长度超过一定限制，尝试转换为Markdown以获得更好的结构
        if len(plain_text) > 500:
            try:
                # 处理可能的HTML内容在plain_text中的情况
                if re.search(r'<[a-zA-Z]+[^>]*>', plain_text):
                    formatted_text = html_to_markdown(plain_text, as_plain_text=True)
                    logger.info("检测到plain_text中可能包含HTML，已提取纯文本")
                else:
                    # 简单格式化，保留换行符
                    formatted_text = plain_text
            except Exception as e:
                logger.error(f"格式化文本时出错: {e}")
                # 如果格式化失败，使用原始文本
                formatted_text = plain_text
        else:
            try:
                # 转义纯文本以避免HTML注入
                escaped_text = plain_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                # 保留换行符
                formatted_text = escaped_text.replace("\n", "<br>")
            except Exception as e:
                logger.error(f"转义文本时出错: {e}")
                # 如果转义失败，使用简单处理
                formatted_text = plain_text
        
        # 创建一个简单的HTML模板，确保正确编码和样式
        html_content = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; line-height: 1.5; color: #333;">
            <h2 style="margin-bottom: 16px; color: #333;">{subject}</h2>
            <div style="white-space: pre-wrap; font-size: 14px; padding: 10px; background-color: #f8f9fa; border-radius: 4px;">
                {formatted_text}
            </div>
        </div>
        """
        logger.info(f"已从纯文本创建HTML内容，长度: {len(html_content)} 字符")
    elif not html_content and not plain_text:
        logger.error("HTML和纯文本内容均为空，无法创建预览")
        # 创建一个简单的提示HTML
        html_content = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; line-height: 1.5; color: #333; text-align: center; padding: 20px;">
            <h2 style="margin-bottom: 16px; color: #333;">{subject}</h2>
            <div style="font-size: 16px; padding: 20px; background-color: #f8f9fa; border-radius: 4px; border: 1px solid #dee2e6;">
                <p>此邮件没有可显示的内容。</p>
                <p>这可能是一个空邮件或只包含无法显示的内容。</p>
            </div>
        </div>
        """
        logger.info("已创建空内容提示HTML")
    
    # 添加超时保护 - 限制整个转换过程不超过20秒
    try:
        # 使用asyncio.wait_for设置严格的超时限制
        logger.info("开始HTML转图片处理，设置20秒超时保护")
        try:
            # 使用asyncio.wait_for确保不会超过20秒
            image_bytes = await asyncio.wait_for(
                html_to_image(html_content),
                timeout=20.0  # 20秒总超时（包含networkidle状态的10秒等待时间）
            )
            if not image_bytes:
                logger.error("HTML转图片失败，无法创建文档")
                return None
            
            # 生成一个有意义的文件名
            # 截取主题前30个字符，确保文件名合法
            safe_subject = "".join([c for c in subject if c.isalnum() or c in " -_"])
            safe_subject = safe_subject.strip()[:30]
            if not safe_subject:
                safe_subject = "email_preview"
            
            filename = f"{safe_subject}_preview.jpg"
            logger.info(f"生成的文件名: {filename}, 文档大小: {len(image_bytes)} 字节")
            
            return (image_bytes, filename)
        except asyncio.TimeoutError:
            logger.error("HTML转图片操作超时(20秒)，强制终止处理")
            # 超时错误，确保返回None，让调用方能正确处理超时情况
            return None
    except Exception as e:
        logger.error(f"创建文档时发生错误: {e}")
        logger.error(traceback.format_exc())
        return None 

# 新增：阻止外部资源加载的路由处理函数
async def _block_external_resources(route):
    """
    路由请求处理函数，用于阻止外部资源加载
    只允许加载data:URL和内部资源
    """
    if route.request.url.startswith('data:'):
        # 允许加载data:URL（如内联图片）
        await route.continue_()
    else:
        # 阻止其他所有资源加载
        await route.abort()

# 新增：预处理HTML内容，将<img>标签替换为占位符
def _preprocess_html_for_rendering(html_content: str) -> str:
    """
    预处理HTML内容，将外部资源替换为占位符
    """
    if not html_content:
        return html_content
    
    # 将<img>标签替换为占位符，保留alt属性内容
    def replace_img(match):
        alt_match = re.search(r'alt=["\'](.*?)["\']', match.group(0))
        alt_text = alt_match.group(1) if alt_match else "图片"
        return f'<div class="img-placeholder">[图片: {alt_text}]</div>'
    
    # 替换所有图片标签，除了data:URL格式的内联图片
    pattern = r'<img(?!\s+src="data:)[^>]*>'
    processed_html = re.sub(pattern, replace_img, html_content)
    
    # 如果发现有iframe，也替换为占位符
    processed_html = re.sub(r'<iframe[^>]*>.*?</iframe>', 
                           '<div class="img-placeholder">[嵌入内容]</div>', 
                           processed_html, 
                           flags=re.DOTALL)
    
    return processed_html 