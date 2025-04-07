"""
国际化支持模块。
提供多语言翻译支持，使应用能够支持不同语言的用户界面。
"""

import os
import json
import logging
from typing import Dict, Optional

# 配置日志
logger = logging.getLogger(__name__)

# 当前语言
current_language = "zh_CN"

# 翻译数据
translations: Dict[str, Dict[str, str]] = {}

def load_translations(lang_code: str) -> None:
    """
    加载指定语言代码的翻译文件
    
    Args:
        lang_code: 语言代码 (例如 'zh_CN', 'en_US')
    """
    global translations, current_language
    
    # 设置翻译文件路径
    i18n_dir = os.path.dirname(os.path.abspath(__file__))
    translation_file = os.path.join(i18n_dir, f"{lang_code}.json")
    
    try:
        if os.path.exists(translation_file):
            with open(translation_file, 'r', encoding='utf-8') as file:
                translations[lang_code] = json.load(file)
            logger.info(f"已加载语言: {lang_code}, 共 {len(translations[lang_code])} 个翻译项")
            current_language = lang_code
        else:
            logger.warning(f"翻译文件不存在: {translation_file}")
    except Exception as e:
        logger.error(f"加载翻译文件出错: {e}")
        # 确保字典至少有一个空条目
        translations[lang_code] = {}

def set_language(lang_code: str) -> bool:
    """
    设置当前使用的语言
    
    Args:
        lang_code: 语言代码 (例如 'zh_CN', 'en_US')
    
    Returns:
        bool: 是否成功设置语言
    """
    global current_language
    
    if lang_code not in translations:
        load_translations(lang_code)
    
    if lang_code in translations:
        current_language = lang_code
        logger.info(f"当前语言已设置为: {lang_code}")
        return True
    
    logger.warning(f"无法设置语言: {lang_code}, 找不到相应的翻译文件")
    return False

def _(key: str, lang_code: Optional[str] = None) -> str:
    """
    获取翻译文本
    
    Args:
        key: 翻译键
        lang_code: 可选语言代码，如果为None则使用当前语言
    
    Returns:
        str: 翻译后的文本，如果找不到翻译则返回原始key
    """
    lang = lang_code or current_language
    
    # 如果语言未加载，尝试加载
    if lang not in translations:
        load_translations(lang)
    
    # 如果找不到翻译，返回原始key
    if lang not in translations or key not in translations[lang]:
        return key
    
    return translations[lang][key]

# 初始化时加载默认语言
load_translations(current_language) 