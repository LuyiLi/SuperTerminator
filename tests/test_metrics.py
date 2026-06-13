from app.metrics import (
    CPU_COMMAND,
    DISK_COMMAND,
    GPU_QUERY_COMMAND,
    MEMORY_COMMAND,
    parse_cpu_percent,
    parse_disk_lines,
    parse_gpu_csv,
    parse_memory_line,
)


def test_parse_gpu_csv_returns_gpu_metric_dicts_and_skips_malformed_rows():
    raw = (
        "A100-SXM4-80GB, 81920 MiB, 1024 MiB, 50 %\n"
        "malformed,row\n"
        "NoInts, total, used, util\n"
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
            "name": "NoInts",
            "memory_total_mib": 0,
            "memory_used_mib": 0,
            "utilization_gpu_percent": 0,
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


def test_parse_memory_line_defaults_missing_values_to_zero():
    assert parse_memory_line("") == {
        "total_kib": 0,
        "available_kib": 0,
        "used_percent": 0.0,
    }


def test_parse_cpu_percent_returns_rounded_float():
    assert parse_cpu_percent("42.74") == 42.7


def test_metric_commands_match_task_5_spec():
    assert (
        GPU_QUERY_COMMAND
        == "nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits"
    )
    assert CPU_COMMAND == r"LC_ALL=C top -bn1 | awk '/Cpu\(s\)/ {print 100 - $8}'"
    assert MEMORY_COMMAND == "cat /proc/meminfo | grep -E 'MemTotal|MemAvailable'"
    assert (
        DISK_COMMAND
        == "df -h --output=source,size,used,avail,pcent,target | tail -n +2"
    )


def test_parse_disk_lines_skips_header_and_malformed_rows():
    raw = (
        "Filesystem Size Used Avail Use% Mounted on\n"
        "filesystem Size Used Avail Use% Mounted on\n"
        "/dev/sda1 7.0T 3.2T 3.8T 46% /data\n"
        "malformed row"
    )

    assert parse_disk_lines(raw) == [
        {
            "filesystem": "/dev/sda1",
            "size": "7.0T",
            "used": "3.2T",
            "avail": "3.8T",
            "use_percent": "46%",
            "mount": "/data",
        }
    ]
