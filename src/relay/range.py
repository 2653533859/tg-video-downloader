"""HTTP range parsing for relay streaming."""


def parse_range(range_header, total_size, chunk_size=4 * 1024 * 1024):
    if not range_header:
        end = min(chunk_size - 1, max(total_size - 1, 0))
        return 0, end, 206
    if not range_header.startswith("bytes=") or "," in range_header:
        raise ValueError("invalid range")

    raw = range_header[6:]
    start_str, end_str = raw.split("-", 1)
    if start_str == "":
        length = int(end_str)
        if length <= 0:
            raise ValueError("invalid range")
        start = max(total_size - length, 0)
        end = total_size - 1
    else:
        start = int(start_str)
        end = min(start + chunk_size - 1, total_size - 1) if end_str == "" else int(end_str)

    if start < 0 or end < start or start >= total_size:
        raise ValueError("invalid range")
    return start, min(end, total_size - 1), 206
