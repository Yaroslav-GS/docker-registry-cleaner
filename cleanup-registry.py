#!/usr/bin/env python3
import requests
import json
import logging
import builtins
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import subprocess
import sys
import os

def load_config(config_file='config.json'):
    """Load configuration settings from a JSON file."""
    with open(config_file, 'r') as f:
        return json.load(f)

# Load the configuration
config = load_config()

REGISTRY_URL = config['registry']['url']
REGISTRY_USER = config['registry']['user']
REGISTRY_PASSWORD = config['registry']['password']
REGISTRY_CONTAINER = config['registry']['container']
DAYS_TO_KEEP = config['cleanup']['days_to_keep']
PROTECTED_TAGS = config['cleanup']['protected_tags']
PROTECTED_PATTERNS = config['cleanup'].get('protected_patterns', [])
PATHS_CONFIG = config['paths']
CONFIG_PATH = PATHS_CONFIG['config']
REGISTRY_STORAGE_PATH = PATHS_CONFIG.get('storage', '/var/lib/registry')
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
LOG_FILE_PATH = '/var/logs/clean-registry.log'
ORIGINAL_PRINT = builtins.print


def setup_logging():
    logger = logging.getLogger('clean_registry')
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        try:
            log_dir = os.path.dirname(LOG_FILE_PATH)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(LOG_FILE_PATH)
        except OSError as exc:
            ORIGINAL_PRINT(f"Warning: unable to set up log file {LOG_FILE_PATH}: {exc}")
        else:
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.DEBUG if DEBUG else logging.INFO)
            logger.addHandler(file_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


LOGGER = setup_logging()


def _log_print(*args, **kwargs):
    # Mirror stdout to the log file while preserving original behavior.
    message = kwargs.get('sep', ' ').join(str(arg) for arg in args)
    end = kwargs.get('end', '\n')
    if end is not None and end != '\n':
        message = f"{message}{end}"

    if LOGGER:
        LOGGER.log(logging.DEBUG if DEBUG else logging.INFO, message)

    ORIGINAL_PRINT(*args, **kwargs)


builtins.print = _log_print

def get_auth():
    """Return the basic-auth credentials tuple if available."""
    if REGISTRY_USER and REGISTRY_PASSWORD:
        return (REGISTRY_USER, REGISTRY_PASSWORD)
    return None

def get_repositories():
    """Retrieve the list of all repositories."""
    url = f"{REGISTRY_URL}/v2/_catalog"
    response = requests.get(url, auth=get_auth())
    response.raise_for_status()
    return response.json().get('repositories', [])

def get_tags(repository):
    """Retrieve the list of tags for a repository."""
    url = f"{REGISTRY_URL}/v2/{repository}/tags/list"
    response = requests.get(url, auth=get_auth())
    response.raise_for_status()
    return response.json().get('tags', [])

def get_image_created_date(repository, tag):
    """Get the image creation date from the config blob or manifest."""
    try:
        url = f"{REGISTRY_URL}/v2/{repository}/manifests/{tag}"
        
        manifest_types = [
            'application/vnd.oci.image.manifest.v1+json',
            'application/vnd.docker.distribution.manifest.v2+json',
            'application/vnd.docker.distribution.manifest.v1+json',
        ]
        
        manifest = None
        digest = None
        response = None
        
        for media_type in manifest_types:
            headers = {'Accept': media_type}
            response = requests.get(url, headers=headers, auth=get_auth())
            
            if response.status_code == 404:
                return None, None
            
            if response.status_code == 200:
                digest = response.headers.get('Docker-Content-Digest')
                manifest = response.json()
                if DEBUG:
                    print(f"    DEBUG: Got manifest type: {media_type}")
                break
        
        if manifest is None:
            return None, None
        
        # Handle manifest lists (multi-arch)
        if manifest.get('mediaType') in [
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.oci.image.index.v1+json'
        ]:
            if 'manifests' in manifest and len(manifest['manifests']) > 0:
                first_manifest_digest = manifest['manifests'][0]['digest']
                response = requests.get(
                    f"{REGISTRY_URL}/v2/{repository}/manifests/{first_manifest_digest}",
                    headers={'Accept': 'application/vnd.oci.image.manifest.v1+json'},
                    auth=get_auth()
                )
                if response.status_code == 200:
                    manifest = response.json()
        
        # Method 1: use the config blob for the date
        if 'config' in manifest:
            config_digest = manifest['config']['digest']
            config_url = f"{REGISTRY_URL}/v2/{repository}/blobs/{config_digest}"
            config_response = requests.get(config_url, auth=get_auth())
            
            if config_response.status_code == 200:
                config = config_response.json()
                
                if DEBUG:
                    print(f"    DEBUG: Config keys: {list(config.keys())}")
                
                # Check various fields that may contain the date
                created_str = config.get('created')
                if created_str:
                    created_date = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                    return digest, created_date.replace(tzinfo=None)
                
                # Fall back to history entries
                if 'history' in config and len(config['history']) > 0:
                    for hist in config['history']:
                        if 'created' in hist:
                            created_str = hist['created']
                            created_date = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                            return digest, created_date.replace(tzinfo=None)
        
        # Method 2: fall back to Last-Modified on the manifest
        if response:
            last_modified = response.headers.get('Last-Modified')
            if last_modified:
                if DEBUG:
                    print(f"    DEBUG: Using Last-Modified: {last_modified}")
                date = parsedate_to_datetime(last_modified)
                return digest, date.replace(tzinfo=None)
        
        # Method 3: check v1 compatibility data
        if 'history' in manifest:
            for history_entry in manifest.get('history', []):
                if 'v1Compatibility' in history_entry:
                    v1_compat = json.loads(history_entry['v1Compatibility'])
                    created_str = v1_compat.get('created')
                    if created_str:
                        created_date = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                        return digest, created_date.replace(tzinfo=None)
        
        return digest, None
        
    except Exception as e:
        if DEBUG:
            print(f"    DEBUG Error: {e}")
            import traceback
            traceback.print_exc()
        return None, None

def delete_tag(repository, digest):
    """Delete a tag by its manifest digest."""
    url = f"{REGISTRY_URL}/v2/{repository}/manifests/{digest}"
    response = requests.delete(url, auth=get_auth())
    if response.status_code == 202:
        return True
    return False

def format_size(num_bytes):
    """Convert a byte count into a human-readable string."""
    if num_bytes is None:
        return "unknown"

    step = 1024.0
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    size = float(num_bytes)

    for unit in units:
        if size < step or unit == units[-1]:
            if unit == 'B':
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= step

    return f"{size:.2f} PB"

def get_registry_disk_usage():
    """Return the registry storage size in bytes, if available."""
    host_storage_path = PATHS_CONFIG.get('host_storage')
    if host_storage_path and os.path.exists(host_storage_path):
        try:
            result = subprocess.run(
                ['du', '-sb', host_storage_path],
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )
            size_str = result.stdout.strip().split()[0]
            size = int(size_str)
            LOGGER.info(f"Registry storage size (host path): {format_size(size)}")
            return size
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
            LOGGER.warning(f"Failed to get size via host path {host_storage_path}: {exc}")
            if DEBUG:
                print(f"  DEBUG: Host path measurement failed: {exc}")
    
    try:
        check_cmd = ['docker', 'inspect', '-f', '{{.State.Running}}', REGISTRY_CONTAINER]
        check_result = subprocess.run(
            check_cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if check_result.returncode != 0:
            LOGGER.error(f"Container {REGISTRY_CONTAINER} not found or not accessible")
            print(f"Error: Container {REGISTRY_CONTAINER} not found")
            return None
        
        is_running = check_result.stdout.strip()
        if is_running != 'true':
            LOGGER.error(f"Container {REGISTRY_CONTAINER} is not running")
            print(f"Error: Container {REGISTRY_CONTAINER} is not running")
            return None
        
        du_cmd = ['docker', 'exec', REGISTRY_CONTAINER, 'du', '-sb', REGISTRY_STORAGE_PATH]
        result = subprocess.run(
            du_cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60
        )
        
        output = result.stdout.strip()
        if not output:
            LOGGER.warning("du command returned empty output")
            return None
        
        size_str = output.split()[0]
        size = int(size_str)
        LOGGER.info(f"Registry storage size (container): {format_size(size)}")
        return size
        
    except subprocess.TimeoutExpired:
        LOGGER.error("Timeout while measuring registry storage size")
        print("Error: Timeout while measuring storage size")
        return None
    except subprocess.CalledProcessError as exc:
        LOGGER.error(f"Failed to execute du in container: {exc.stderr if exc.stderr else exc}")
        print(f"Error: Failed to measure storage size")
        return None
    except (ValueError, IndexError) as exc:
        LOGGER.error(f"Failed to parse du output: {exc}")
        return None
    except Exception as exc:
        LOGGER.exception("Unexpected error measuring registry storage")
        print(f"Error: {exc}")
        return None


def run_garbage_collection_docker(dry_run=False):
    """Run the registry garbage collector through docker exec."""
    cmd = ['docker', 'exec', REGISTRY_CONTAINER, 'registry', 'garbage-collect', 
           CONFIG_PATH, '--delete-untagged']
    if dry_run:
        cmd.append('--dry-run')
    
    try:
        LOGGER.debug("Executing garbage collection command: %s", ' '.join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout and DEBUG:
            LOGGER.debug("Garbage collector stdout:\n%s", result.stdout.strip())
        if result.stderr:
            LOGGER.warning("Garbage collector stderr: %s", result.stderr.strip())

        if result.returncode != 0:
            LOGGER.error("Garbage collection exited with code %s", result.returncode)
        else:
            LOGGER.info("Garbage collection command completed successfully")

        return result.returncode == 0
    except Exception as e:
        LOGGER.exception("Error running garbage collection")
        print(f"Error running garbage collection: {e}")
        return False

def main():
    print(f"Starting Docker Registry cleanup...")
    print(f"Registry: {REGISTRY_URL}")
    print(f"Protected tags: {', '.join(PROTECTED_TAGS)}")
    print(f"Deleting tags older than {DAYS_TO_KEEP} days")
    if DEBUG:
        print(f"Debug mode: ON\n")
    else:
        print()


    deleted_count = 0
    skipped_count = 0
    before_usage = get_registry_disk_usage()
    if before_usage is not None:
        print(f"Registry storage size before cleanup: {format_size(before_usage)} ({before_usage} bytes)")
    else:
        print("Registry storage size before cleanup: unavailable")

    cutoff_date = datetime.now() - timedelta(days=DAYS_TO_KEEP)
    
    try:
        repositories = get_repositories()
        print(f"Found {len(repositories)} repositories\n")
        
        for repo in repositories:
            print(f"\nProcessing repository: {repo}")
            tags = get_tags(repo)
            
            if not tags:
                print(f"  No tags found")
                continue
            
            print(f"  Found {len(tags)} tags")
            
            for tag in tags:
                tag_lower = tag.lower()

                # Skip protected tags
                if tag in PROTECTED_TAGS:
                    print(f"  ✓ Keeping protected tag: {tag}")
                    continue
            
                if any(pattern in tag_lower for pattern in PROTECTED_PATTERNS):
                    print(f" ✓ Keeping protected tag: {tag} - pattern match")
                    continue

                # Skip special tags
                if tag in ['buildcache', 'latest', 'cache']:
                    print(f"  ⊘ Skipping special tag: {tag}")
                    skipped_count += 1
                    continue
                
                digest, created_date = get_image_created_date(repo, tag)
                
                if digest is None:
                    print(f"  ! Skipping tag (not found): {tag}")
                    skipped_count += 1
                    continue
                
                if created_date is None:
                    print(f"  ! Skipping tag (no date info): {tag}")
                    skipped_count += 1
                    continue
                
                # Calculate the age of the image
                age_days = (datetime.now() - created_date).days
                
                if created_date < cutoff_date:
                    print(f"  × Deleting tag: {tag} (created: {created_date.strftime('%Y-%m-%d %H:%M')}, age: {age_days} days)")
                    if delete_tag(repo, digest):
                        deleted_count += 1
                        print(f"    ✓ Successfully deleted")
                    else:
                        print(f"    × Failed to delete")
                else:
                    print(f"  ✓ Keeping recent tag: {tag} (created: {created_date.strftime('%Y-%m-%d %H:%M')}, age: {age_days} days)")
        
        print(f"\n{'='*50}")
        print(f"Total tags deleted: {deleted_count}")
        print(f"Total tags skipped: {skipped_count}")
        print(f"{'='*50}\n")
        
        if deleted_count > 0:
            print("Running garbage collection...")
            if run_garbage_collection_docker():
                print("✓ Garbage collection completed successfully")
            else:
                print("× Garbage collection failed")
        else:
            print("No tags deleted, skipping garbage collection")
            
    except Exception as e:
        LOGGER.exception("Unhandled error during cleanup")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print(f"\n{'='*50}")
        print("POST-CLEANUP STORAGE MEASUREMENT")
        print(f"{'='*50}")
        
        if deleted_count > 0:
            print("Waiting for filesystem sync...")
            try:
                subprocess.run(
                    ['docker', 'exec', REGISTRY_CONTAINER, 'sync'],
                    timeout=10,
                    capture_output=True
                )
            except Exception:
                pass
        
        after_usage = get_registry_disk_usage()
        
        if after_usage is not None:
            print(f"Registry storage size after cleanup: {format_size(after_usage)} ({after_usage} bytes)")
            if before_usage is not None:
                diff = before_usage - after_usage
                if diff > 0:
                    percent = (diff / before_usage) * 100
                    print(f"✓ Freed space: {format_size(diff)} ({diff} bytes, {percent:.2f}%)")
                elif diff < 0:
                    diff_abs = abs(diff)
                    print(f"⚠ Storage increased: {format_size(diff_abs)} ({diff_abs} bytes)")
                else:
                    print("No space freed (0 bytes)")
            else:
                print("Cannot calculate freed space (initial size unavailable)")
        else:
            print("Registry storage size after cleanup: unavailable")
        
        print(f"{'='*50}\n")


if __name__ == '__main__':
    main()
