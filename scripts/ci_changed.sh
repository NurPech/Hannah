# Shared helper, sourced by CI jobs to skip work when their component didn't
# change since the previous release tag. Reads a flag file written by the
# `detect-changes` job (needs: detect-changes, which downloads its `changes/`
# artifact by default) instead of a dotenv report variable — GitLab evaluates
# rules: at pipeline creation time, before any job (including detect-changes)
# has run, so a dotenv variable from an earlier job is never actually
# available there (see #267). A plain file artifact consumed at runtime in
# script:/before_script: doesn't have that problem, and works in every job
# image regardless of whether it has git installed (several of the images
# used here — kaniko, skopeo, curl — don't).

# Usage: ci_skip_if_unchanged <dir> — call at the top of a job's script,
# where <dir> matches one of the top-level dirs detect-changes diffs (core,
# proxy, satellite-esp, telegram, voiceid). Exits 0 immediately if <dir>
# hasn't changed, so the job shows as successful without doing anything else.
ci_skip_if_unchanged() {
  dir="$1"
  if [ "${FORCE_PUBLISH:-}" = "true" ]; then
    return 0
  fi
  if [ "$(cat "changes/${dir}" 2>/dev/null)" != "true" ]; then
    echo "No changes in ${dir} since previous tag — skipping."
    exit 0
  fi
}
