from app.metrics import (
    parse_cpu_percent,
    parse_disk_lines,
    parse_gpu_csv,
    parse_memory_line,
)


def test_parse_gpu_csv_returns_gpu_metric_dicts():
    raw = (
        "A100-SXM4-80GB, 81920 MiB, 1024 MiB, 50 %\n"
        "A100-SXM4-80GB, 81920 MiB, 2048 MiB, 75 %"
    )

    assert parse_gpu_csv(raw) == [
        {
            "name": "A100-SXM4-80GB",
            "memory_total_mib": 81920,
            "memory_used_mib": 1024,
            "utilization_gpu_percent": 50,
        },
        {
            "name": "A100-SXM4-80GB",
            "memory_total_mib": 81920,
            "memory_used_mib": 2048,
            "utilization_gpu_percent": 75,
        },
    ]


def test_parse_memory_line_returns_total_available_and_used_percent():
    raw = "MemTotal: 263000000 kB\nMemAvailable: 131500000 kB"

    assert parse_memory_line(raw) == {
        "total_kib": 263000000,
        "available_kib": 131500000,
        "used_percent": 50.0,
    }


def test_parse_cpu_percent_returns_float():
    assert parse_cpu_percent("42.7") == 42.7


def test_parse_disk_lines_parses_df_header_and_data_rows():
    raw = "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 7.0T 3.2T 3.8T 46% /data"

    assert parse_disk_lines(raw) == [
        {
            "filesystem": "/dev/sda1",
            "size": "7.0T",
            "used": "3.2T",
            "available": "3.8T",
            "used_percent": 46,
            "mountpoint": "/data",
        }
    ]
