#!/usr/bin/env python3
"""
TDLib setup script for development environment.

This script automatically sets up TDLib library files for local development
across different platforms (macOS, Linux, Windows).
"""

import argparse
import sys
import os
from pathlib import Path

# Add the app directory to Python path to import our modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.tdlib_manager import get_tdlib_manager
from app.utils import Logger

logger = Logger().get_logger(__name__)


def main():
    """Main entry point for the TDLib setup script."""
    parser = argparse.ArgumentParser(
        description="Setup TDLib library files for development environment"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force recreation of library files even if they exist"
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Show platform and library information"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate current library setup"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Set log level
    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)
    
    manager = get_tdlib_manager()
    
    if args.info:
        print_platform_info(manager)
        return
    
    if args.validate:
        validate_setup(manager)
        return
    
    # Default action: setup development libraries
    setup_development_libraries(manager, args.force)


def print_platform_info(manager):
    """Print detailed platform and library information."""
    info = manager.get_platform_info()
    
    print("=== TDLib Platform Information ===")
    print(f"Platform: {info['platform']}")
    print(f"Architecture: {info['architecture']}")
    print(f"Container Environment: {info['is_container']}")
    print()
    print("=== Library Paths ===")
    print(f"Source Library: {info['source_library']}")
    print(f"Bot Library: {info['bot_library']}")
    print(f"User Library: {info['user_library']}")
    print()
    
    # Check if files exist
    print("=== File Status ===")
    source_exists = os.path.exists(info['source_library'])
    bot_exists = os.path.exists(info['bot_library'])
    user_exists = os.path.exists(info['user_library'])
    
    print(f"Source Library: {'✓' if source_exists else '✗'} {info['source_library']}")
    print(f"Bot Library: {'✓' if bot_exists else '✗'} {info['bot_library']}")
    print(f"User Library: {'✓' if user_exists else '✗'} {info['user_library']}")


def validate_setup(manager):
    """Validate the current TDLib setup."""
    print("=== Validating TDLib Setup ===")
    
    is_valid = manager.validate_library_setup()
    
    if is_valid:
        print("✓ TDLib setup is valid")
        sys.exit(0)
    else:
        print("✗ TDLib setup is invalid")
        print("\nRun 'python scripts/setup_tdlib.py' to fix the setup")
        sys.exit(1)


def setup_development_libraries(manager, force=False):
    """Setup development libraries."""
    print("=== Setting up TDLib for Development ===")
    
    platform_name, arch = manager.platform_info
    print(f"Detected platform: {platform_name}_{arch}")
    
    if platform_name == 'windows':
        print("❌ Windows development environment detected.")
        print("📝 Please manually compile TDLib for Windows or use WSL/Docker for development.")
        print("🔗 TDLib compilation guide: https://tdlib.github.io/td/build.html")
        sys.exit(1)
    
    success = manager.setup_development_libraries(force=force)
    
    if success:
        print("✅ TDLib development libraries setup completed successfully!")
        print("\n📋 Next steps:")
        print("1. Make sure your .env file is configured with Telegram credentials")
        print("2. Run 'python -m app.main' to start the application")
        print("3. Use 'python scripts/setup_tdlib.py --validate' to verify setup")
    else:
        print("❌ Failed to setup TDLib development libraries")
        print("\n🔍 Troubleshooting:")
        print("1. Check if the source TDLib library file exists:")
        source_path = manager.get_source_library_path()
        print(f"   {source_path}")
        print("2. Ensure you have the correct TDLib library for your platform")
        print("3. Check file permissions in the app/resources/tdlib/ directory")
        sys.exit(1)


if __name__ == "__main__":
    main()
