import os, json
from app.utils import Logger
from app.utils.decorators import Singleton

logger = Logger().get_logger(__name__)

DATA_DIR = os.path.join(os.getcwd(), "data")


@Singleton
class DataManager:

    @staticmethod
    def get_groups():
        """
        Retrieve group data from the 'group.txt' file.

        Returns:
            dict: A dictionary containing group data if successfully loaded,
                otherwise an empty dictionary in case of an error.
        """

        group_file_path = os.path.join(DATA_DIR, "group.txt")
        if not os.path.exists(group_file_path):
            return {}
        with open(group_file_path, "r") as f:
            groups_data = json.load(f)
        return groups_data

    @staticmethod
    def save_groups(groups):
        group_file_path = os.path.join(DATA_DIR, "group.txt")
        with open(group_file_path, "w+") as f:
            json.dump(groups, f)

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
