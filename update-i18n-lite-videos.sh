#!/bin/bash -xe

# This script is run by the jenkins 'update-i18n-lite-videos' in order to
# query youtube to get the videos information from our i18n lite youtube
# channels and add them to intl/translations/videos_*.json

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd -P )"
WORKSPACE_ROOT=`pwd -P`
source "${SCRIPT_DIR}/build.lib"
ensure_virtualenv
decrypt_secrets_py_and_add_to_pythonpath

cd "$WEBSITE_ROOT"

"$MAKE" install_deps

# --- The actual work:

# This lets us commit messages without a test plan
export FORCE_COMMIT=1

echo "Updating intl/translations."
safe_pull intl/translations

echo "Updating the list of videos we have for 'lite' languages."
tools/update_i18n_lite_videos.py intl/translations

echo "Checking in new video-lists."
safe_commit_and_push intl/translations \
   -m "Automatic update of video_*.json" \
   -m "(at webapp commit `git rev-parse HEAD`)"


