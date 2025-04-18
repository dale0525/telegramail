import json
import os
import re
from pathlib import Path
from collections import defaultdict

# Define project root and relevant paths
# Define the root directory of the project.
# This assumes the script is run from the project root or the 'scripts' directory.
project_root = Path(__file__).parent.parent

# Define paths for i18n directory
i18n_dir = project_root / "app" / "i18n"
# Removed hardcoded file paths: zh_cn_file, en_us_file

# Define the directory containing source code to scan.
app_dir = project_root / "app"

# Regular expression to find i18n usage like _('key') or _("key")
# It captures the key within the quotes.
i18n_func_pattern = re.compile(r'_\([\'"](.+?)[\'"]\)')
# Regular expression to find i18n usage in dicts like "some_key": "i18n_key"
# Corrected: Use [\'"] to match either single or double quote.
i18n_dict_pattern = re.compile(r'[\'"](\w+_key)[\'"]\s*:\s*[\'"](.+?)[\'"]')

def load_json_data(file_path):
    """Loads data from a JSON file."""
    # Reads a JSON file and returns its content as a dictionary.
    # Handles potential file not found or JSON decoding errors.
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Warning: File not found - {file_path}. Creating an empty structure.")
        return {}
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from - {file_path}. Returning empty structure.")
        return {}

def save_json_data(file_path, data):
    """Saves data to a JSON file with pretty printing."""
    # Writes a dictionary to a JSON file with UTF-8 encoding and 4-space indentation.
    # Sorts keys alphabetically for consistency.
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            # Sort keys before dumping for consistent file output
            sorted_data = dict(sorted(data.items()))
            json.dump(sorted_data, f, ensure_ascii=False, indent=4)
        print(f"Successfully saved changes to {file_path}")
    except IOError as e:
        print(f"Error saving file {file_path}: {e}")

def find_used_keys(directory):
    """Finds all i18n keys used in Python files within a directory, ignoring comments."""
    # Recursively scans a directory for .py files.
    # Extracts i18n keys using multiple predefined regex patterns, skipping lines starting with '#'.
    # Returns a set of unique keys found in the code.
    used_keys = set()
    for filepath in Path(directory).rglob('*.py'):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # content = f.read() # Removed reading whole file
                for line in f: # Read line by line
                    stripped_line = line.strip()
                    # Skip empty lines and lines that start with a comment marker '#'
                    if not stripped_line or stripped_line.startswith('#'):
                        continue

                    # --- Apply patterns only to non-comment lines ---
                    # Find matches using the _('key') pattern in the current line
                    func_matches = i18n_func_pattern.findall(line)
                    used_keys.update(func_matches) # Add found keys to the set

                    # Find matches using the "..._key": "value" pattern in the current line
                    dict_matches = i18n_dict_pattern.findall(line)
                    # For dict matches, the key is the second element in the tuple
                    used_keys.update(match[1] for match in dict_matches)

        except Exception as e:
            print(f"Error reading file {filepath}: {e}")
    return used_keys

if __name__ == "__main__":
    print(f"Loading i18n data from {i18n_dir}...")
    # Scan for all .json files in the i18n directory
    i18n_files = list(Path(i18n_dir).glob('*.json'))
    i18n_data_map = {} # Store data keyed by file path

    if not i18n_files:
        print(f"Error: No JSON files found in {i18n_dir}. Exiting.")
        exit(1)

    # Load data from all found JSON files and collect all defined keys
    all_defined_keys = set()
    for file_path in i18n_files:
        print(f" -> Loading {file_path.name}")
        data = load_json_data(file_path)
        i18n_data_map[file_path] = data
        all_defined_keys.update(data.keys())

    print(f"\nFound {len(all_defined_keys)} unique defined keys across {len(i18n_files)} JSON file(s).")

    print(f"\nScanning '{app_dir}' for used i18n keys...")
    # Find all keys actually used in the source code using both patterns
    used_keys_in_code = find_used_keys(app_dir)
    print(f"Found {len(used_keys_in_code)} used keys in code.")

    # --- Key Management ---
    # Calculate unused keys: defined in JSON but not found in code.
    unused_keys = all_defined_keys - used_keys_in_code
    # Calculate undefined keys: used in code but not defined in JSON.
    undefined_keys = used_keys_in_code - all_defined_keys

    made_changes = False

    # 1. Delete unused keys
    if unused_keys:
        print("\nDeleting unused i18n keys:")
        for key in sorted(list(unused_keys)):
            print(f"- {key}")
            # Remove the key from all loaded dictionaries
            for data in i18n_data_map.values():
                if key in data:
                    del data[key]
            made_changes = True
    else:
        print("\nNo unused i18n keys to delete.")

    # 2. Add undefined keys
    if undefined_keys:
        print("\nAdding undefined i18n keys (with empty translation):")
        for key in sorted(list(undefined_keys)):
            print(f"+ {key}")
            # Add the key to all loaded dictionaries with an empty string value
            for data in i18n_data_map.values():
                 # Only add if it doesn't already exist (though it shouldn't by definition)
                 if key not in data:
                    data[key] = ""
            made_changes = True
    else:
        print("\nNo undefined keys to add.")

    # --- Save Changes ---
    if made_changes:
        print("\nSaving updated i18n files...")
        # Save changes back to each original file
        for file_path, data in i18n_data_map.items():
            save_json_data(file_path, data)
    else:
        print("\nNo changes made to i18n files.") 