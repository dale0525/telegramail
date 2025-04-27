import os, json
from app.utils import Logger
from app.utils.decorators import Singleton
from app.database import DBManager

logger = Logger().get_logger(__name__)

DATA_DIR = os.path.join(os.getcwd(), "data")


@Singleton
class DataManager:
    def __init__(self):
        self.db_manager = DBManager()

    @staticmethod
    def get_folder_id():
        folder_file_path = os.path.join(DATA_DIR, "folder.txt")
        if not os.path.exists(folder_file_path):
            return None
        with open(folder_file_path, "r") as f:
            folder_id = f.read().strip()
        if folder_id == "":
            return None
        return int(folder_id)

    @staticmethod
    def save_folder_id(folder_id: int | str):
        folder_file_path = os.path.join(DATA_DIR, "folder.txt")
        with open(folder_file_path, "w+") as f:
            f.write(str(folder_id))
