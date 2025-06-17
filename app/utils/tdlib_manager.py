"""
TDLib library file management utility for cross-platform support.

This module handles automatic detection, copying, and configuration of TDLib library files
for different platforms (macOS, Linux AMD64, Linux ARM64) and environments (development, production).
"""

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple
from app.utils import Logger

logger = Logger().get_logger(__name__)


class TDLibManager:
    """Manages TDLib library files across different platforms and environments."""
    
    def __init__(self, base_path: Optional[str] = None):
        """
        Initialize TDLib manager.
        
        Args:
            base_path: Base path for the application. Defaults to current working directory.
        """
        self.base_path = Path(base_path or os.getcwd())
        self.tdlib_dir = self.base_path / "app" / "resources" / "tdlib"
        self.darwin_dir = self.tdlib_dir / "darwin"
        
        # Platform detection
        self.platform_info = self._detect_platform()
        
    def _detect_platform(self) -> Tuple[str, str]:
        """
        Detect current platform and architecture.
        
        Returns:
            Tuple of (platform, architecture)
        """
        system = platform.system().lower()
        machine = platform.machine().lower()
        
        # Normalize architecture names
        if machine in ['x86_64', 'amd64']:
            arch = 'amd64'
        elif machine in ['aarch64', 'arm64']:
            arch = 'arm64'
        else:
            arch = machine
            
        return system, arch
    
    def get_library_filename(self, platform_name: str, arch: str) -> str:
        """
        Get the library filename for a specific platform and architecture.
        
        Args:
            platform_name: Platform name (darwin, linux, windows)
            arch: Architecture (amd64, arm64)
            
        Returns:
            Library filename
        """
        if platform_name == 'darwin':
            return f"libtdjson_{platform_name}_{arch}.dylib"
        elif platform_name == 'linux':
            return f"libtdjson_{platform_name}_{arch}.so"
        elif platform_name == 'windows':
            return f"tdjson_{platform_name}_{arch}.dll"
        else:
            raise ValueError(f"Unsupported platform: {platform_name}")
    
    def get_source_library_path(self, platform_name: Optional[str] = None, arch: Optional[str] = None) -> Path:
        """
        Get the source library file path for the specified or current platform.
        
        Args:
            platform_name: Target platform name. Defaults to current platform.
            arch: Target architecture. Defaults to current architecture.
            
        Returns:
            Path to the source library file
        """
        if platform_name is None or arch is None:
            platform_name, arch = self.platform_info
            
        filename = self.get_library_filename(platform_name, arch)
        return self.tdlib_dir / filename
    
    def get_development_library_paths(self, platform_name: Optional[str] = None, arch: Optional[str] = None) -> Tuple[Path, Path]:
        """
        Get the development library file paths (bot and user clients need separate copies).
        
        Args:
            platform_name: Target platform name. Defaults to current platform.
            arch: Target architecture. Defaults to current architecture.
            
        Returns:
            Tuple of (bot_library_path, user_library_path)
        """
        if platform_name is None or arch is None:
            platform_name, arch = self.platform_info
            
        if platform_name == 'darwin':
            bot_path = self.darwin_dir / f"libtdjson_darwin_{arch}_1.dylib"
            user_path = self.darwin_dir / f"libtdjson_darwin_{arch}_2.dylib"
        elif platform_name == 'linux':
            # For Linux, we'll create a similar structure
            linux_dir = self.tdlib_dir / "linux"
            bot_path = linux_dir / f"libtdjson_linux_{arch}_1.so"
            user_path = linux_dir / f"libtdjson_linux_{arch}_2.so"
        else:
            raise ValueError(f"Development setup not supported for platform: {platform_name}")
            
        return bot_path, user_path
    
    def setup_development_libraries(self, force: bool = False) -> bool:
        """
        Set up library files for local development environment.
        
        Args:
            force: Force recreation of library files even if they exist
            
        Returns:
            True if setup was successful, False otherwise
        """
        platform_name, arch = self.platform_info
        
        if platform_name == 'windows':
            logger.warning("Windows development environment detected.")
            logger.warning("Please manually compile TDLib for Windows or use WSL/Docker for development.")
            return False
            
        try:
            source_path = self.get_source_library_path()
            
            if not source_path.exists():
                logger.error(f"Source library file not found: {source_path}")
                logger.error(f"Please ensure TDLib library for {platform_name}_{arch} is available.")
                return False
                
            bot_path, user_path = self.get_development_library_paths()
            
            # Create target directory if it doesn't exist
            bot_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Check if files already exist and are not forced to recreate
            if not force and bot_path.exists() and user_path.exists():
                logger.info(f"Development libraries already exist for {platform_name}_{arch}")
                return True
                
            # Copy source library to bot and user specific files
            shutil.copy2(source_path, bot_path)
            shutil.copy2(source_path, user_path)
            
            logger.info(f"Successfully set up development libraries for {platform_name}_{arch}")
            logger.info(f"Bot library: {bot_path}")
            logger.info(f"User library: {user_path}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to setup development libraries: {e}")
            return False
    
    def get_runtime_library_path(self, client_type: str = "bot") -> str:
        """
        Get the runtime library path for the specified client type.
        
        Args:
            client_type: Type of client ("bot" or "user")
            
        Returns:
            Absolute path to the library file as string
        """
        platform_name, arch = self.platform_info
        
        # In production/container environment, use the source library directly
        if self._is_container_environment():
            source_path = self.get_source_library_path()
            return str(source_path.absolute())
        
        # In development environment, use separate library files
        try:
            bot_path, user_path = self.get_development_library_paths()
            
            if client_type == "bot":
                target_path = bot_path
            elif client_type == "user":
                target_path = user_path
            else:
                raise ValueError(f"Invalid client type: {client_type}")
                
            # Ensure development libraries are set up
            if not target_path.exists():
                logger.info(f"Development library not found, setting up...")
                if not self.setup_development_libraries():
                    # Fallback to source library
                    logger.warning("Falling back to source library")
                    return str(self.get_source_library_path().absolute())
                    
            return str(target_path.absolute())
            
        except Exception as e:
            logger.error(f"Failed to get runtime library path: {e}")
            # Fallback to source library
            return str(self.get_source_library_path().absolute())
    
    def _is_container_environment(self) -> bool:
        """
        Check if running in a container environment.
        
        Returns:
            True if running in container, False otherwise
        """
        # Check for common container indicators
        return (
            os.path.exists('/.dockerenv') or
            os.environ.get('CONTAINER') == 'true' or
            os.environ.get('DOCKER_CONTAINER') == 'true' or
            'docker' in os.environ.get('HOSTNAME', '').lower()
        )
    
    def validate_library_setup(self) -> bool:
        """
        Validate that library setup is correct for the current environment.
        
        Returns:
            True if setup is valid, False otherwise
        """
        try:
            bot_path = self.get_runtime_library_path("bot")
            user_path = self.get_runtime_library_path("user")
            
            bot_exists = os.path.exists(bot_path)
            user_exists = os.path.exists(user_path)
            
            if not bot_exists:
                logger.error(f"Bot library not found: {bot_path}")
            if not user_exists:
                logger.error(f"User library not found: {user_path}")
                
            return bot_exists and user_exists
            
        except Exception as e:
            logger.error(f"Library validation failed: {e}")
            return False
    
    def get_platform_info(self) -> dict:
        """
        Get detailed platform information.
        
        Returns:
            Dictionary containing platform details
        """
        platform_name, arch = self.platform_info
        
        return {
            'platform': platform_name,
            'architecture': arch,
            'is_container': self._is_container_environment(),
            'source_library': str(self.get_source_library_path()),
            'bot_library': self.get_runtime_library_path("bot"),
            'user_library': self.get_runtime_library_path("user"),
        }


# Global instance for easy access
_tdlib_manager = None


def get_tdlib_manager() -> TDLibManager:
    """Get the global TDLib manager instance."""
    global _tdlib_manager
    if _tdlib_manager is None:
        _tdlib_manager = TDLibManager()
    return _tdlib_manager


def setup_tdlib_for_development(force: bool = False) -> bool:
    """
    Convenience function to set up TDLib for development environment.
    
    Args:
        force: Force recreation of library files
        
    Returns:
        True if setup was successful, False otherwise
    """
    manager = get_tdlib_manager()
    return manager.setup_development_libraries(force=force)


def get_library_path(client_type: str = "bot") -> str:
    """
    Convenience function to get library path for a client type.
    
    Args:
        client_type: Type of client ("bot" or "user")
        
    Returns:
        Absolute path to the library file
    """
    manager = get_tdlib_manager()
    return manager.get_runtime_library_path(client_type)
