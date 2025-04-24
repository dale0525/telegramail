import os
import json
import re
from typing import Dict, Optional, Any
from dotenv import load_dotenv
from app.utils import Logger

logger = Logger().get_logger(__name__)
load_dotenv()

current_language = os.environ.get("DEFAULT_LANGUAGE", "en_US")

translations: Dict[str, Dict[str, str]] = {}


def load_translations(lang_code: str) -> None:
    """
    load translation file for lang_code

    Args:
        lang_code: language code (eg. 'zh_CN', 'en_US')
    """
    global translations, current_language

    # set translation file path
    i18n_dir = os.path.dirname(os.path.abspath(__file__))
    translation_file = os.path.join(i18n_dir, f"{lang_code}.json")

    try:
        if os.path.exists(translation_file):
            with open(translation_file, "r", encoding="utf-8") as file:
                translations[lang_code] = json.load(file)
            logger.info(
                f"{lang_code} is loaded. {len(translations[lang_code])} translations in total."
            )
            current_language = lang_code
        else:
            logger.warning(f"no translation for {lang_code}")
    except Exception as e:
        logger.error(f"failed to load translation file: {e}")
        # set empty dict
        translations[lang_code] = {}


def set_language(lang_code: str) -> bool:
    """
    set current language

    Args:
        lang_code: language code (eg. 'zh_CN', 'en_US')

    Returns:
        bool: success or not
    """
    global current_language

    if lang_code not in translations:
        load_translations(lang_code)

    if lang_code in translations:
        current_language = lang_code
        logger.info(f"current language is set to {lang_code}")
        return True

    logger.warning(f"cannot set language to {lang_code}: no translation file found")
    return False


def _(key: str, lang_code: Optional[str] = None, **kwargs) -> str:
    """
    get translations with placeholder replacement

    Args:
        key: translation key
        lang_code: language code. if set to None, current_language is used
        **kwargs: variables for placeholder replacement

    Returns:
        str: translated text with placeholders replaced. if not found, key is returned
    """
    lang = lang_code or current_language

    if lang not in translations:
        load_translations(lang)

    if lang not in translations or key not in translations[lang]:
        return key

    text = translations[lang][key]
    
    # Replace placeholders if kwargs are provided
    if kwargs:
        for key, value in kwargs.items():
            placeholder = "{" + key + "}"
            text = text.replace(placeholder, str(value))
    
    return text


# load default language translation when initialized
load_translations(current_language)
