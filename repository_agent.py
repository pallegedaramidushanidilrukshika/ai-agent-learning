import os
import json


IGNORED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "target",
    "bin",
    "obj",
    "dist",
    "build",
}


def scan_repository(repo_path):
    """Scan repository and return relevant files."""

    files = []

    for root, dirs, filenames in os.walk(repo_path):

        dirs[:] = [
            directory
            for directory in dirs
            if directory not in IGNORED_DIRS
        ]

        for filename in filenames:

            if filename.startswith("."):
                continue

            full_path = os.path.join(root, filename)

            relative_path = os.path.relpath(
                full_path,
                repo_path
            )

            files.append(relative_path)

    return files


def detect_project_type(files):
    """Detect project technologies and basic execution information."""

    file_names = {
        os.path.basename(file).lower()
        for file in files
    }

    detected = []

    # ---------------------------------------------------------
    # NODE.JS
    # ---------------------------------------------------------

    if "package.json" in file_names:

        detected.append({
            "technology": "Node.js",
            "language": "JavaScript",
            "config_file": "package.json",
            "package_manager": "npm"
        })

    # ---------------------------------------------------------
    # PYTHON
    # ---------------------------------------------------------

    if (
        "requirements.txt" in file_names
        or "pyproject.toml" in file_names
        or "setup.py" in file_names
    ):

        detected.append({
            "technology": "Python",
            "language": "Python",
            "package_manager": "pip"
        })

    # ---------------------------------------------------------
    # JAVA
    # ---------------------------------------------------------

    if (
        "pom.xml" in file_names
        or "build.gradle" in file_names
        or "build.gradle.kts" in file_names
    ):

        package_manager = "Maven"

        if (
            "build.gradle" in file_names
            or "build.gradle.kts" in file_names
        ):
            package_manager = "Gradle"

        detected.append({
            "technology": "Java",
            "language": "Java",
            "package_manager": package_manager
        })

    # ---------------------------------------------------------
    # .NET
    # ---------------------------------------------------------

    if any(
        filename.endswith(".csproj")
        or filename.endswith(".sln")
        for filename in file_names
    ):

        detected.append({
            "technology": ".NET",
            "language": "C#",
            "package_manager": "NuGet"
        })

    # ---------------------------------------------------------
    # REACT
    # ---------------------------------------------------------

    if (
        "package.json" in file_names
        and any(
            filename.endswith(".jsx")
            or filename.endswith(".tsx")
            for filename in file_names
        )
    ):

        detected.append({
            "technology": "React",
            "language": "JavaScript/TypeScript",
            "framework": "React"
        })

    # ---------------------------------------------------------
    # ANGULAR
    # ---------------------------------------------------------

    if "angular.json" in file_names:

        detected.append({
            "technology": "Angular",
            "language": "TypeScript",
            "framework": "Angular"
        })

    # ---------------------------------------------------------
    # UNKNOWN
    # ---------------------------------------------------------

    if not detected:

        detected.append({
            "technology": "Unknown",
            "language": "Unknown"
        })

    return detected


def read_file(repo_path, relative_path):
    """Read a file from the target repository."""

    full_path = os.path.join(
        repo_path,
        relative_path
    )

    try:
        with open(
            full_path,
            "r",
            encoding="utf-8"
        ) as file:

            return file.read()

    except Exception as error:

        return f"Unable to read file: {error}"


def find_important_files(files):
    """Identify files that are useful for understanding the project."""

    important_files = []

    important_names = {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "angular.json",
        "readme.md",
    }

    backend_entry_files = {
        "index.js",
        "app.js",
        "server.js",
        "main.js",
        "main.py",
        "app.py",
        "program.cs",
    }

    for file in files:

        filename = os.path.basename(file).lower()

        # Project configuration files
        if filename in important_names:
            important_files.append(file)

        # Common application entry points
        if filename in backend_entry_files:
            important_files.append(file)

        # Frontend/template files
        extension = os.path.splitext(filename)[1]

        if extension in {
            ".ejs",
            ".html",
            ".jsx",
            ".tsx",
            ".vue",
        }:
            important_files.append(file)

    return list(dict.fromkeys(important_files))


def build_repository_context(repo_path):

    files = scan_repository(repo_path)

    project_types = detect_project_type(files)

    important_files = find_important_files(files)

    context = {
        "repository": os.path.abspath(repo_path),
        "project_types": project_types,
        "file_count": len(files),
        "files": files,
        "important_files": important_files,
        "file_contents": {},
    }

    # Read important files
    for file in important_files:

        context["file_contents"][file] = read_file(
            repo_path,
            file
        )

    return context


if __name__ == "__main__":

    target_repo = (
        r"C:\Users\SINGER\OneDrive\Desktop"
        r"\target-projects\nodejs-todo"
    )

    context = build_repository_context(
        target_repo
    )

        # Save repository context to a JSON file
    output_file = "repository_context.json"

    with open(output_file, "w", encoding="utf-8") as file:
        json.dump(context, file, indent=2)

    print(f"\nRepository context saved to: {output_file}")
    
        # Save repository context for the AI diagnosis agent
    with open(
        "repository_context.json",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            context,
            file,
            indent=2
        )

    print(
        "\nRepository context saved to repository_context.json"
    )

    print("\n===== REPOSITORY ANALYSIS =====")

    print(
        "Repository:",
        context["repository"]
    )

    print(
        "Detected project type:",
        ", ".join(
        project["technology"]
        for project in context["project_types"]
        )
    )

    print(
        "Total files:",
        context["file_count"]
    )

    print("\n===== IMPORTANT FILES =====")

    for file in context["important_files"]:
        print(file)

    print("\n===== FILE CONTENT SUMMARY =====")

    for file, content in context["file_contents"].items():

        print(
            f"\n--- {file} ---"
        )

        print(content)

    print("\n===== REPOSITORY CONTEXT JSON =====")

    print(
        json.dumps(
            context,
            indent=2
        )
    )
