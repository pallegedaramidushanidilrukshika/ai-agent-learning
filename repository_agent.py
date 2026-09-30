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
    "nodejs-todo-ai-debugging.git"
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


def read_json_file(repo_path, relative_path):
    """Read a JSON file from the target repository."""

    try:
        with open(
            os.path.join(repo_path, relative_path),
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def package_manifests(files):
    return [
        file
        for file in files
        if os.path.basename(file).lower() == "package.json"
    ]


def files_in_directory(files, directory):
    normalized_directory = directory.replace("\\", "/").strip("/")

    if not normalized_directory:
        return files

    prefix = f"{normalized_directory}/"
    return [
        file
        for file in files
        if file.replace("\\", "/").startswith(prefix)
    ]


def package_manager_for_path(files, directory):
    directory_files = {
        os.path.basename(file).lower()
        for file in files_in_directory(files, directory)
    }

    if "pnpm-lock.yaml" in directory_files:
        return "pnpm"

    if "yarn.lock" in directory_files:
        return "yarn"

    if "bun.lockb" in directory_files or "bun.lock" in directory_files:
        return "bun"

    return "npm"


def package_dependencies(manifest):
    dependencies = {}

    for key in ("dependencies", "devDependencies", "peerDependencies"):
        values = manifest.get(key, {})
        if isinstance(values, dict):
            dependencies.update({str(name).lower(): value for name, value in values.items()})

    return dependencies


def detect_project_type(files, repo_path=None):
    """Detect technologies and frameworks from repository evidence."""

    file_names = {
        os.path.basename(file).lower()
        for file in files
    }

    detected = []

    manifests = []
    for manifest_path in package_manifests(files):
        manifest = (
            read_json_file(repo_path, manifest_path)
            if repo_path
            else {}
        )
        manifests.append((manifest_path, manifest))

    if manifests:
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

    all_dependencies = set()
    for _, manifest in manifests:
        all_dependencies.update(package_dependencies(manifest))

    has_react_files = any(
        filename.endswith(".jsx") or filename.endswith(".tsx")
        for filename in file_names
    )

    if "react" in all_dependencies or has_react_files:
        detected.append({
            "technology": "React",
            "language": "JavaScript/TypeScript",
            "framework": "React",
        })

    if "vite" in all_dependencies or any(
        filename.startswith("vite.config.")
        for filename in file_names
    ):
        detected.append({
            "technology": "Vite",
            "language": "JavaScript/TypeScript",
            "framework": "Vite",
        })

    if "express" in all_dependencies:
        detected.append({
            "technology": "Express",
            "language": "JavaScript",
            "framework": "Express",
        })

    if {"mongodb", "mongoose"} & all_dependencies:
        detected.append({
            "technology": "MongoDB",
            "language": "Database",
            "framework": None,
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


def command_from_script(manifest, script_name):
    scripts = manifest.get("scripts", {})
    script = scripts.get(script_name) if isinstance(scripts, dict) else None

    if not isinstance(script, str) or not script.strip():
        return None

    return f"npm run {script_name}" if script_name != "start" else "npm start"


def component_from_manifest(repo_path, files, manifest_path):
    manifest = read_json_file(repo_path, manifest_path)
    dependencies = package_dependencies(manifest)
    directory = os.path.dirname(manifest_path)
    directory_files = files_in_directory(files, directory)
    directory_names = {
        os.path.basename(file).lower()
        for file in directory_files
    }

    has_react = "react" in dependencies or any(
        file.lower().endswith((".jsx", ".tsx"))
        for file in directory_files
    )
    has_vite = "vite" in dependencies or any(
        name.startswith("vite.config.")
        for name in directory_names
    )
    has_express = "express" in dependencies
    has_server_entry = bool(
        directory_names & {"index.js", "app.js", "server.js", "main.js"}
    )

    if has_react:
        component_type = "frontend"
        technology = "React"
        framework = "Vite" if has_vite else "React"
        language = "TypeScript" if any(
            file.lower().endswith(".tsx")
            for file in directory_files
        ) else "JavaScript"
    elif has_express or has_server_entry or "server" in directory.lower():
        component_type = "backend"
        technology = "Node.js"
        framework = "Express" if has_express else None
        language = "TypeScript" if any(
            file.lower().endswith(".ts")
            for file in directory_files
        ) else "JavaScript"
    else:
        component_type = "application"
        technology = "Node.js"
        framework = None
        language = "TypeScript" if any(
            file.lower().endswith(".ts")
            for file in directory_files
        ) else "JavaScript"

    scripts = manifest.get("scripts", {})
    has_test_script = isinstance(scripts, dict) and isinstance(
        scripts.get("test"),
        str,
    )
    start_command = command_from_script(manifest, "start")
    if not start_command and component_type == "frontend":
        start_command = command_from_script(manifest, "dev")
    if not start_command and component_type != "frontend":
        main_file = manifest.get("main")
        if isinstance(main_file, str) and main_file.strip():
            start_command = f"node {main_file}"

    build_command = command_from_script(manifest, "build")
    test_command = command_from_script(manifest, "test") if has_test_script else None

    if component_type == "frontend" and not test_command:
        test_command = command_from_script(manifest, "lint")

    package_manager = package_manager_for_path(files, directory)

    return {
        "name": os.path.basename(directory) if directory else "application",
        "type": component_type,
        "technology": technology,
        "framework": framework,
        "language": language,
        "path": directory.replace("\\", "/") or ".",
        "package_manager": package_manager,
        "install_command": f"{package_manager} install",
        "start_command": start_command,
        "test_command": test_command,
        "build_command": build_command,
        "validation_method": (
            "HTTP / Browser"
            if component_type == "frontend"
            else "HTTP / CLI"
        ),
        "test_framework": "Cypress" if "cypress" in dependencies else None,
        "evidence": [manifest_path],
    }


def detect_architecture(repo_path, files, project_types):
    """Build an evidence-based architecture and component command model."""

    components = [
        component_from_manifest(repo_path, files, manifest_path)
        for manifest_path in package_manifests(files)
    ]

    project_file_names = {
        os.path.basename(file).lower()
        for file in files
    }
    project_text = "\n".join(
        read_file(repo_path, file).lower()
        for file in files
        if os.path.basename(file).lower() in {
            "readme.md",
            "server.js",
            "app.js",
            "connection.js",
            "serverless.yml",
            "serverless.yaml",
            "template.yaml",
        }
    )

    all_dependencies = set()
    for manifest_path in package_manifests(files):
        all_dependencies.update(
            package_dependencies(read_json_file(repo_path, manifest_path))
        )

    if {"mongodb", "mongoose"} & all_dependencies or re.search(
        r"mongodb(?:\+srv)?://|mongodb\.com|mongoclient",
        project_text,
    ):
        components.append({
            "name": "database",
            "type": "database",
            "technology": "MongoDB",
            "framework": None,
            "language": None,
            "path": None,
            "package_manager": None,
            "install_command": None,
            "start_command": None,
            "test_command": None,
            "build_command": None,
            "validation_method": "Database connectivity",
            "test_framework": None,
            "evidence": [
                "package.json"
                if {"mongodb", "mongoose"} & all_dependencies
                else "source/configuration"
            ],
        })

    infrastructure_evidence = []
    infrastructure = None
    for filename, name in (
        ("serverless.yml", "Serverless Framework"),
        ("serverless.yaml", "Serverless Framework"),
        ("template.yaml", "AWS SAM"),
        ("cdk.json", "AWS CDK"),
        ("main.tf", "Terraform"),
    ):
        if filename in project_file_names:
            infrastructure = name
            infrastructure_evidence.append(filename)

    if infrastructure:
        components.append({
            "name": "infrastructure",
            "type": "infrastructure",
            "technology": infrastructure,
            "framework": infrastructure,
            "language": None,
            "path": None,
            "package_manager": None,
            "install_command": None,
            "start_command": None,
            "test_command": None,
            "build_command": None,
            "validation_method": "Infrastructure validation",
            "test_framework": None,
            "evidence": infrastructure_evidence,
        })

    component_types = {component["type"] for component in components}
    if "frontend" in component_types and (
        "backend" in component_types or "database" in component_types
    ):
        application_type = "full_stack"
    elif "infrastructure" in component_types and (
        "backend" in component_types or "application" in component_types
    ):
        application_type = "serverless"
    elif len(components) <= 1:
        application_type = "single_runtime"
    else:
        application_type = "multi_component"

    return {
        "application_type": application_type,
        "components": components,
    }


def detect_execution_strategy(project_types):
    """Select commands based on detected project technology."""

    technologies = {
        project["technology"]
        for project in project_types
    }

    if (
        "Node.js" in technologies
        and "React" in technologies
        and "Express" in technologies
        and "MongoDB" in technologies
    ):
        return {
            "technology": "MERN",
            "frontend": "React",
            "frontend_framework": "Vite",
            "backend": "Node.js / Express",
            "database": "MongoDB",
            "package_manager": "npm",
            "install_command": "npm install",
            "start_command": "npm start",
            "backend_path": "mern/server",
            "backend_install_command": "npm install",
            "backend_start_command": "npm start",
            "backend_base_url": "http://localhost:5050",
            "test_command": "npm test",
            "build_command": "npm run build",
            "validation_method": "HTTP / Browser",
            "test_framework": "Cypress / Jest",
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
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "pyproject.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "angular.json",
        "tsconfig.json",
        "serverless.yml",
        "serverless.yaml",
        "template.yaml",
        "cdk.json",
        "main.tf",
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
    project_types = detect_project_type(files, repo_path)
    important_files = find_important_files(files)
    execution_strategy = detect_execution_strategy(project_types)
    architecture = detect_architecture(
        repo_path,
        files,
        project_types,
    )

    context = {
        "repository_url": repository_url,
        "repository_path": os.path.abspath(repo_path),
        "project_types": project_types,
        "execution_strategy": execution_strategy,
        "architecture": architecture,
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
