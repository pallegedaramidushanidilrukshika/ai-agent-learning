import json
import os
import re
import subprocess
import sys
from urllib.parse import urlparse


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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_PROJECTS_DIR = os.path.join(
    os.path.dirname(BASE_DIR),
    "target-projects",
)
OUTPUT_FILE = os.path.join(BASE_DIR, "repository_context.json")
DEFAULT_REPOSITORY_URL = (
    "https://github.com/"
    "pallegedaramidushanidilrukshika/"
    "ai-bug-fixing-agent-demo.git"
)


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
            relative_path = os.path.relpath(full_path, repo_path)
            files.append(relative_path)

    return files


def detect_project_type(files):
    """Detect project technology and framework."""

    file_names = {
        os.path.basename(file).lower()
        for file in files
    }

    detected = []

    if "package.json" in file_names:
        detected.append({
            "technology": "Node.js",
            "language": "JavaScript",
            "config_file": "package.json",
            "package_manager": "npm",
        })

    if (
        "requirements.txt" in file_names
        or "pyproject.toml" in file_names
        or "setup.py" in file_names
    ):
        detected.append({
            "technology": "Python",
            "language": "Python",
            "package_manager": "pip",
        })

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
            "package_manager": package_manager,
        })

    if any(
        filename.endswith(".csproj")
        or filename.endswith(".sln")
        for filename in file_names
    ):
        detected.append({
            "technology": ".NET",
            "language": "C#",
            "package_manager": "NuGet",
        })

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
            "framework": "React",
        })

    if "angular.json" in file_names:
        detected.append({
            "technology": "Angular",
            "language": "TypeScript",
            "framework": "Angular",
        })

    if not detected:
        detected.append({
            "technology": "Unknown",
            "language": "Unknown",
        })

    return detected


def detect_execution_strategy(project_types):
    """Select commands based on detected project technology."""

    technologies = {
        project["technology"]
        for project in project_types
    }

    if "Node.js" in technologies:
        return {
            "technology": "Node.js",
            "package_manager": "npm",
            "install_command": "npm install",
            "start_command": "node index.js",
            "test_command": "node --test",
            "build_command": None,
            "validation_method": "HTTP",
            "test_framework": "node:test",
        }

    if "Python" in technologies:
        return {
            "technology": "Python",
            "package_manager": "pip",
            "install_command": "pip install -r requirements.txt",
            "start_command": None,
            "test_command": "pytest",
            "build_command": None,
            "validation_method": "CLI / HTTP",
            "test_framework": "pytest",
        }

    if "Java" in technologies:
        if any(
            project.get("package_manager") == "Maven"
            for project in project_types
        ):
            return {
                "technology": "Java",
                "package_manager": "Maven",
                "install_command": "mvn install",
                "start_command": None,
                "test_command": "mvn test",
                "build_command": "mvn package",
                "validation_method": "CLI / HTTP",
                "test_framework": "JUnit",
            }

        return {
            "technology": "Java",
            "package_manager": "Gradle",
            "install_command": "gradle build",
            "start_command": None,
            "test_command": "gradle test",
            "build_command": "gradle build",
            "validation_method": "CLI / HTTP",
            "test_framework": "JUnit",
        }

    if ".NET" in technologies:
        return {
            "technology": ".NET",
            "package_manager": "NuGet",
            "install_command": "dotnet restore",
            "start_command": "dotnet run",
            "test_command": "dotnet test",
            "build_command": "dotnet build",
            "validation_method": "CLI / HTTP",
            "test_framework": "xUnit / NUnit / MSTest",
        }

    if "Angular" in technologies:
        return {
            "technology": "Angular",
            "package_manager": "npm",
            "install_command": "npm install",
            "start_command": "ng serve",
            "test_command": "ng test --watch=false",
            "build_command": "ng build",
            "validation_method": "HTTP / Browser",
            "test_framework": "Jasmine / Karma",
        }

    if "React" in technologies:
        return {
            "technology": "React",
            "package_manager": "npm",
            "install_command": "npm install",
            "start_command": "npm start",
            "test_command": "npm test -- --watchAll=false",
            "build_command": "npm run build",
            "validation_method": "HTTP / Browser",
            "test_framework": "Jest / React Testing Library",
        }

    return {
        "technology": "Unknown",
        "package_manager": None,
        "install_command": None,
        "start_command": None,
        "test_command": None,
        "build_command": None,
        "validation_method": "Manual",
        "test_framework": None,
    }


def read_file(repo_path, relative_path):
    """Read a file from the target repository."""

    full_path = os.path.join(repo_path, relative_path)

    try:
        with open(full_path, "r", encoding="utf-8") as file:
            return file.read()
    except Exception as error:
        return f"Unable to read file: {error}"


def find_important_files(files):
    """Identify files useful for understanding the project."""

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

        if filename in important_names:
            important_files.append(file)

        if filename in backend_entry_files:
            important_files.append(file)

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


def get_repository_url():
    """Get the repository URL from arguments or the environment."""

    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()

    environment_url = os.getenv("GITHUB_REPOSITORY_URL")
    if environment_url and environment_url.strip():
        return environment_url.strip()

    return DEFAULT_REPOSITORY_URL


def repository_directory_name(repository_url):
    """Derive a safe local directory name from a repository URL."""

    parsed = urlparse(repository_url)
    name = os.path.basename(parsed.path.rstrip("/"))

    if name.endswith(".git"):
        name = name[:-4]

    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")

    if not name:
        raise ValueError("Could not derive a local repository name from the URL.")

    return name


def choose_clone_directory(repository_url):
    """Choose a new directory without modifying an existing clone."""

    os.makedirs(TARGET_PROJECTS_DIR, exist_ok=True)

    base_name = repository_directory_name(repository_url)
    candidate = os.path.join(TARGET_PROJECTS_DIR, base_name)
    suffix = 1

    while os.path.exists(candidate):
        candidate = os.path.join(
            TARGET_PROJECTS_DIR,
            f"{base_name}-{suffix}",
        )
        suffix += 1

    return os.path.abspath(candidate)


def clone_repository(repository_url):
    """Clone a repository into a new local directory."""

    clone_path = choose_clone_directory(repository_url)

    result = subprocess.run(
        ["git", "clone", repository_url, clone_path],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git clone failed with exit code {result.returncode}: "
            f"{details}"
        )

    if not os.path.isdir(clone_path):
        raise RuntimeError(
            "git clone completed without creating the expected directory."
        )

    return clone_path


def build_repository_context(repo_path, repository_url):
    files = scan_repository(repo_path)
    project_types = detect_project_type(files)
    important_files = find_important_files(files)
    execution_strategy = detect_execution_strategy(project_types)

    context = {
        "repository_url": repository_url,
        "repository_path": os.path.abspath(repo_path),
        "project_types": project_types,
        "execution_strategy": execution_strategy,
        "file_count": len(files),
        "files": files,
        "important_files": important_files,
        "file_contents": {},
    }

    for file in important_files:
        context["file_contents"][file] = read_file(repo_path, file)

    return context


def main():
    repository_url = get_repository_url()

    try:
        repository_path = clone_repository(repository_url)
        context = build_repository_context(
            repository_path,
            repository_url,
        )

        with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
            json.dump(context, file, indent=2, ensure_ascii=False)

    except (OSError, ValueError, RuntimeError) as error:
        error_context = {
            "status": "FAILED",
            "repository_url": repository_url,
            "repository_path": None,
            "error": str(error),
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
            json.dump(error_context, file, indent=2, ensure_ascii=False)

        print(json.dumps(error_context, indent=2, ensure_ascii=False))
        return 1

    print("\n===== REPOSITORY ANALYSIS =====")
    print("Repository URL:", context["repository_url"])
    print("Repository:", context["repository_path"])
    print(
        "Detected technologies:",
        ", ".join(
            project["technology"]
            for project in context["project_types"]
        ),
    )

    print("\n===== EXECUTION STRATEGY =====")
    print(json.dumps(context["execution_strategy"], indent=2))
    print("\nRepository context saved to:", OUTPUT_FILE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
