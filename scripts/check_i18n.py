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

# Regular expression to find i18n usage like _('key') and _('key', param=value)
# This pattern captures the key within quotes regardless of whether parameters follow
i18n_func_pattern = re.compile(r'_\([\'"](.+?)[\'"](,|\))')

# Regular expression to find dynamic i18n usage like _(f"prefix_{var}") and keep the
# static prefix ("prefix_") so we don't delete keys that are referenced via f-strings.
dynamic_i18n_fstring_prefix_pattern = re.compile(r'_\(\s*f[\'"]([^\'"{]+)\{')

# Regular expression to find i18n usage in dicts like "some_key": "i18n_key"
# Corrected: Use [\'"] to match either single or double quote.
i18n_dict_pattern = re.compile(r'[\'"](\w+_key)[\'"]\s*:\s*[\'"](.+?)[\'"]')

# Regular expression to find placeholders in translation strings: {name}
placeholder_pattern = re.compile(r'\{([a-zA-Z0-9_]+)\}')

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

def expand_keys_for_dynamic_prefixes(used_keys, dynamic_prefixes, defined_keys):
    """
    Expand a used-keys set based on dynamic i18n prefixes.

    If code contains _(f"email_category_{category}") we should treat all keys
    starting with "email_category_" as used so they are not auto-deleted.
    """
    expanded = set(used_keys or set())
    for prefix in dynamic_prefixes or set():
        expanded.update({k for k in defined_keys if k.startswith(prefix)})
    return expanded

def find_used_keys_and_params(directory):
    """
    Finds all i18n keys used in Python files within a directory, 
    along with their parameters, ignoring comments.
    """
    # Returns a set of keys and a dict of key -> params
    used_keys = set()
    key_params = defaultdict(set)  # Store parameters used with each key
    dynamic_prefixes = set()
    
    # Read all python files once to collect all usage examples for testing
    i18n_usage_examples = []
    
    for filepath in Path(directory).rglob('*.py'):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):  # Read line by line
                    stripped_line = line.strip()
                    # Skip empty lines and lines that start with a comment marker '#'
                    if not stripped_line or stripped_line.startswith('#'):
                        continue
                    
                    # Store all lines containing i18n usage for debugging
                    if '_(' in line:
                        i18n_usage_examples.append((filepath, line_num, line))
                        
                    # --- Apply patterns only to non-comment lines ---
                    # Find matches using the _('key') or _('key', param=value) pattern
                    func_matches = i18n_func_pattern.findall(line)
                    
                    # For each key, extract it and its parameters
                    for match in func_matches:
                        key = match[0]
                        used_keys.add(key)
                        
                        # Extract parameters by searching for param=value patterns
                        # after the key but before the closing parenthesis
                        key_quoted = re.escape(f"'{key}'") + "|" + re.escape(f'"{key}"')
                        full_call_pattern = re.compile(rf'_\(({key_quoted})(.*?)\)')
                        full_call_match = full_call_pattern.search(line)
                        
                        if full_call_match and full_call_match.group(2) and '=' in full_call_match.group(2):
                            params_text = full_call_match.group(2)
                            param_matches = re.findall(r'([a-zA-Z0-9_]+)\s*=', params_text)
                            for param in param_matches:
                                key_params[key].add(param)

                    # Find dynamic i18n usage like _(f"prefix_{var}") and keep the prefix.
                    dyn_matches = dynamic_i18n_fstring_prefix_pattern.findall(line)
                    for prefix in dyn_matches:
                        if prefix:
                            dynamic_prefixes.add(prefix)

                    # Find matches using the "..._key": "value" pattern in the current line
                    dict_matches = i18n_dict_pattern.findall(line)
                    # For dict matches, the key is the second element in the tuple
                    used_keys.update(match[1] for match in dict_matches)

        except Exception as e:
            print(f"Error reading file {filepath}: {e}")
    
    # Debug: print all i18n calls found with parameters
    for key, params in key_params.items():
        if params:
            print(f"Key '{key}' used with parameters: {params}")

    return used_keys, key_params, dynamic_prefixes

def check_placeholders(i18n_data_map, key_params):
    """
    Checks if all placeholders in translations match parameters used in code.
    Returns a list of warnings.
    """
    warnings = []
    
    # For each translation file
    for file_path, translations in i18n_data_map.items():
        lang_code = file_path.stem  # Get language code from filename (e.g., 'en_US')
        
        # Check each translated string
        for key, translation in translations.items():
            # Skip empty translations
            if not translation:
                continue
            
            # Find all placeholders in the translation
            placeholders_in_translation = placeholder_pattern.findall(translation)
            
            # Skip keys without placeholders
            if not placeholders_in_translation:
                continue
                
            # Check if the key is used in code with parameters
            params_used_in_code = key_params.get(key, set())
            
            # Check for placeholders in translation that aren't in code
            missing_in_code = set(placeholders_in_translation) - params_used_in_code
            if missing_in_code:
                warnings.append(
                    f"[{lang_code}] '{key}' has placeholders {missing_in_code} that are not used in code"
                )
            
            # Check for parameters in code that aren't in translation
            missing_in_translation = params_used_in_code - set(placeholders_in_translation)
            if missing_in_translation:
                warnings.append(
                    f"[{lang_code}] '{key}' is missing placeholders for parameters {missing_in_translation}"
                )
    
    return warnings

def analyze_i18n_file(file_path):
    """Analyze a single i18n file for placeholder usage"""
    data = load_json_data(file_path)
    placeholders_by_key = {}
    
    for key, translation in data.items():
        if not translation:
            continue
            
        placeholders = placeholder_pattern.findall(translation)
        if placeholders:
            placeholders_by_key[key] = placeholders
    
    return placeholders_by_key

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

    # Analyze placeholders in each file
    print("\nAnalyzing translation files for placeholders...")
    for file_path in i18n_files:
        lang_code = file_path.stem
        placeholders_by_key = analyze_i18n_file(file_path)
        print(f" -> {lang_code}: Found {len(placeholders_by_key)} keys with placeholders")
        for key, placeholders in placeholders_by_key.items():
            print(f"    - '{key}': {placeholders}")

    print(f"\nScanning '{app_dir}' for used i18n keys...")
    # Find all keys actually used in the source code using both patterns
    used_keys_in_code, key_params, dynamic_prefixes = find_used_keys_and_params(app_dir)
    used_keys_in_code = expand_keys_for_dynamic_prefixes(
        used_keys_in_code, dynamic_prefixes, all_defined_keys
    )
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

    # 3. Check placeholders
    print("\nChecking placeholders in translations...")
    placeholder_warnings = check_placeholders(i18n_data_map, key_params)
    if placeholder_warnings:
        print("\nPlaceholder warnings:")
        for warning in placeholder_warnings:
            print(f"⚠️  {warning}")
    else:
        print("No placeholder issues found.")

    # --- Save Changes ---
    if made_changes:
        print("\nSaving updated i18n files...")
        # Save changes back to each original file
        for file_path, data in i18n_data_map.items():
            save_json_data(file_path, data)
    else:
        print("\nNo changes made to i18n files.") 
