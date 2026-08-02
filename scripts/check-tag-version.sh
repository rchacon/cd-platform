#!/usr/bin/env bash
# Usage: check-tag-version.sh <tag-name> <tag-prefix> <pyproject-path> [git-ref]
#
# Fails if the version implied by <tag-name> (after stripping <tag-prefix>)
# doesn't match the `version` field in <pyproject-path> as of [git-ref]
# (defaults to HEAD). Shared by the deploy workflows and the local
# pre-push hook so the check logic lives in one place.
set -euo pipefail

tag_name="$1"
tag_prefix="$2"
pyproject_path="$3"
git_ref="${4:-HEAD}"

tag_version="${tag_name#"$tag_prefix"}"
pyproject_version=$(git show "${git_ref}:${pyproject_path}" | sed -n 's/^version = "\(.*\)"/\1/p')

if [ "$tag_version" != "$pyproject_version" ]; then
  echo "error: tag '${tag_name}' implies version '${tag_version}', but ${pyproject_path} (at ${git_ref}) has version '${pyproject_version}'" >&2
  exit 1
fi

echo "OK: tag '${tag_name}' matches ${pyproject_path}'s version '${pyproject_version}'"
