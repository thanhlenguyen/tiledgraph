import os
import sys
from pathlib import Path

def extract_filenames(folder_path: str, include_dirs: bool = False, recursive: bool = False, no_extension: bool = True) -> list[str]:
    """
    Extract filenames from a folder.
    
    Args:
        folder_path: Path to the folder
        include_dirs: Whether to include directory names
        recursive: Whether to search recursively
        no_extension: Whether to remove file extensions
    
    Returns:
        List of filenames (or full paths if recursive)
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Invalid directory: {folder_path}")
    
    filenames = []
    
    if recursive:
        for item in folder.rglob("*"):
            if include_dirs or item.is_file():
                name = str(item.relative_to(folder))
                if no_extension and item.is_file():
                    name = item.stem
                filenames.append(name)
    else:
        for item in folder.iterdir():
            if include_dirs or item.is_file():
                if no_extension and item.is_file():
                    name = item.stem
                else:
                    name = item.name
                filenames.append(name)
    
    return sorted(filenames)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_folder = sys.argv[1]
    else:
        target_folder = "."
    
    try:
        files = extract_filenames(target_folder, include_dirs=False, recursive=False, no_extension=True)
        print(f"Files in '{target_folder}' (without extension):")
        for f in files:
            print(f)
        
        # Optional: save to file
        with open("filenames.txt", "w") as f:
            f.write("\n".join(files))
            
    except Exception as e:
        print(f"Error: {e}")