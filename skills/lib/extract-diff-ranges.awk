# Extract commentable new-file hunk ranges from a unified PR diff.
# Output format: <path>\tL<start>-L<end>
#
# Notes:
# - Use the `+++ b/<path>` file header instead of `diff --git ... b/<path>` so
#   paths containing spaces are preserved.
# - Ignore `+++ /dev/null` deletion headers and zero-length `+n,0` hunks.
# - Once inside a hunk, ignore body lines that begin with `+++ `; in a unified
#   diff those can be added content, not file headers.

BEGIN {
  path = ""
  in_hunk = 0
}

/^diff --git/ {
  path = ""
  in_hunk = 0
  next
}

/^\+\+\+ / && !in_hunk {
  path = ""
  if ($0 ~ /^\+\+\+ b\//) {
    path = substr($0, 7)
  }
  next
}

/^@@/ {
  in_hunk = 1
  if (path == "") {
    next
  }
  if (match($0, /\+[0-9]+,?[0-9]*/) == 0) {
    next
  }
  spec = substr($0, RSTART + 1, RLENGTH - 1)
  n = split(spec, a, ",")
  start = a[1] + 0
  len = (n == 2 ? a[2] + 0 : 1)
  if (len > 0) {
    printf "%s\tL%d-L%d\n", path, start, start + len - 1
  }
}
