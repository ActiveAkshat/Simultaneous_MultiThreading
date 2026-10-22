#!/usr/bin/env python3
import itertools
import os
import re
import statistics
import subprocess
import sys

NUM_JOBS = 10
WARMUP_ITERS = 40
MEAS_ITERS = 250
SOLO_REPEATS = 5
PAIR_REPEATS = 3


def build_binary():
    """Compile main.c with libjobs.a."""
    cmd = ["gcc", "-O2", "-std=c11", "-Wall", "-Wextra", "-D_GNU_SOURCE",
           "main.c", "libjobs.a", "-o", "main", "-pthread"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_kv_line(line):
    data = {}
    for token in line.strip().split():
        if "=" in token:
            k, v = token.split("=", 1)
            data[k] = v
    return data


def run_solo_measurement(serial_no, job_id, cpu_id, warmup, iters):
    """
    Runs ./main under `perf stat` using perf control FIFO/pipes so setup and warmup
    are excluded from perf's counters, and also collects internal per-thread counters.
    """
    r_ctl, w_ctl = os.pipe()
    r_ack, w_ack = os.pipe()
    os.set_inheritable(w_ctl, True)
    os.set_inheritable(r_ack, True)
    os.set_inheritable(r_ctl, True)
    os.set_inheritable(w_ack, True)

    perf_cmd = [
        "perf", "stat", "-x", ",",
        "-e", "instructions,cycles,cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-loads,LLC-load-misses",
        f"--control=fd:{r_ctl},{w_ack}",
        "./main", str(serial_no), "solo", str(job_id), str(cpu_id),
        str(warmup), str(iters), str(w_ctl), str(r_ack)
    ]

    try:
        proc = subprocess.run(
            perf_cmd,
            pass_fds=(r_ctl, w_ctl, r_ack, w_ack),
            capture_output=True,
            text=True,
            timeout=30
        )
        out = proc.stdout
        err = proc.stderr
    except Exception:
        out = ""
        err = ""
    finally:
        for fd in (r_ctl, w_ctl, r_ack, w_ack):
            try:
                os.close(fd)
            except OSError:
                pass

    # Parse internal per-thread exact measurement if available
    ipc = 0.0
    inst = 0
    cycles = 0
    cref = 0
    cmis = 0
    for line in out.splitlines():
        if line.startswith("SOLO "):
            kv = parse_kv_line(line)
            ipc = float(kv.get("ipc", 0.0))
            inst = int(kv.get("inst", 0))
            cycles = int(kv.get("cycles", 0))
            cref = int(kv.get("cref", 0))
            cmis = int(kv.get("cmis", 0))

    # Parse perf stat CSV output from stderr if internal counters were 0 or for extra events
    perf_events = {}
    for line in err.splitlines():
        parts = line.split(",")
        if len(parts) >= 3:
            val_str, _, event_name = parts[0].strip(), parts[1].strip(), parts[2].strip()
            try:
                val = float(val_str)
                perf_events[event_name] = val
            except ValueError:
                pass

    if ipc == 0.0:
        # Fallback to perf stat counters
        p_inst = perf_events.get("instructions", 0.0)
        p_cycl = perf_events.get("cycles", 0.0)
        if p_cycl > 0:
            ipc = p_inst / p_cycl
            inst = int(p_inst)
            cycles = int(p_cycl)
        cref = int(perf_events.get("cache-references", 0))
        cmis = int(perf_events.get("cache-misses", 0))

    # Fallback to direct ./main invocation if `perf stat` failed completely
    if ipc == 0.0:
        proc2 = subprocess.run(
            ["./main", str(serial_no), "solo", str(job_id), str(cpu_id), str(warmup), str(iters)],
            capture_output=True, text=True
        )
        for line in proc2.stdout.splitlines():
            if line.startswith("SOLO "):
                kv = parse_kv_line(line)
                ipc = float(kv.get("ipc", 0.0))
                inst = int(kv.get("inst", 0))
                cycles = int(kv.get("cycles", 0))
                cref = int(kv.get("cref", 0))
                cmis = int(kv.get("cmis", 0))

    return {
        "ipc": ipc,
        "inst": inst,
        "cycles": cycles,
        "cref": cref,
        "cmis": cmis,
        "perf_events": perf_events,
        "perf_raw": err
    }


def run_pair_measurement(serial_no, jobA, jobB, cpuA, cpuB, warmup, iters):
    """
    Measures jobA (pinned to cpuA) and jobB (pinned to cpuB) concurrently.
    If internal perf_event_open counters are available, reads both directly.
    Otherwise falls back to per-CPU perf stat on cpuA and cpuB.
    """
    proc = subprocess.run(
        ["./main", str(serial_no), "pair", str(jobA), str(jobB),
         str(cpuA), str(cpuB), str(warmup), str(iters)],
        capture_output=True, text=True
    )
    ipcA, ipcB = 0.0, 0.0
    for line in proc.stdout.splitlines():
        if line.startswith("PAIR "):
            kv = parse_kv_line(line)
            ipcA = float(kv.get("ipcA", 0.0))
            ipcB = float(kv.get("ipcB", 0.0))

    if ipcA > 0.0 and ipcB > 0.0:
        return ipcA, ipcB

    # Fallback: use `perf stat --per-cpu -C cpuA,cpuB`
    perf_cmd = [
        "perf", "stat", "--per-cpu", "-C", f"{cpuA},{cpuB}", "-x", ",",
        "-e", "instructions,cycles",
        "./main", str(serial_no), "pair", str(jobA), str(jobB),
        str(cpuA), str(cpuB), str(warmup), str(iters)
    ]
    proc2 = subprocess.run(perf_cmd, capture_output=True, text=True)
    cpu_inst = {}
    cpu_cycl = {}
    for line in proc2.stderr.splitlines():
        parts = line.split(",")
        if len(parts) >= 4 and parts[0].startswith("CPU"):
            cpu_num = int(parts[0].replace("CPU", ""))
            try:
                val = float(parts[1])
            except ValueError:
                continue
            ev = parts[3].strip()
            if "instructions" in ev:
                cpu_inst[cpu_num] = val
            elif "cycles" in ev:
                cpu_cycl[cpu_num] = val

    if cpu_cycl.get(cpuA, 0) > 0:
        ipcA = cpu_inst.get(cpuA, 0.0) / cpu_cycl[cpuA]
    if cpu_cycl.get(cpuB, 0) > 0:
        ipcB = cpu_inst.get(cpuB, 0.0) / cpu_cycl[cpuB]

    return ipcA, ipcB


def find_optimal_matching(pair_scores):
    """
    Finds the 5 disjoint pairs of jobs (0..9) that maximize the average pair score.
    Uses dynamic programming over bitmasks of size 2^10 = 1024 (exact global optimum).
    """
    memo = {}

    def solve(mask):
        if mask == (1 << NUM_JOBS) - 1:
            return 0.0, []
        if mask in memo:
            return memo[mask]

        # Find first unmatched job
        first = 0
        while (mask & (1 << first)) != 0:
            first += 1

        best_val = -1.0
        best_pairs = []
        for second in range(first + 1, NUM_JOBS):
            if (mask & (1 << second)) == 0:
                new_mask = mask | (1 << first) | (1 << second)
                sub_val, sub_pairs = solve(new_mask)
                total_val = pair_scores[(first, second)] + sub_val
                if total_val > best_val:
                    best_val = total_val
                    best_pairs = [(first, second)] + sub_pairs

        memo[mask] = (best_val, best_pairs)
        return best_val, best_pairs

    total_score, pairs = solve(0)
    return total_score / 5.0, pairs


def main():
    serial_str = os.environ.get("SERIAL_NO", "28673").strip()
    sibling_str = os.environ.get("SIBLING_CORES", "0,1").strip()

    serial_no = int(serial_str)
    cpus = [int(x.strip()) for x in sibling_str.split(",") if x.strip()]
    if len(cpus) < 2:
        cpus = [0, 1]
    cpuA, cpuB = cpus[0], cpus[1]

    build_binary()

    # Step 1: Measure each job alone on both sibling CPUs
    solo_ipcA = {}
    solo_ipcB = {}
    solo_stats = {}

    for j in range(NUM_JOBS):
        ipcs_a = []
        ipcs_b = []
        last_res = None
        for _ in range(SOLO_REPEATS):
            res_a = run_solo_measurement(serial_no, j, cpuA, WARMUP_ITERS, MEAS_ITERS)
            res_b = run_solo_measurement(serial_no, j, cpuB, WARMUP_ITERS, MEAS_ITERS)
            ipcs_a.append(res_a["ipc"])
            ipcs_b.append(res_b["ipc"])
            last_res = res_a
        solo_ipcA[j] = statistics.median(ipcs_a)
        solo_ipcB[j] = statistics.median(ipcs_b)
        solo_stats[j] = last_res
        solo_stats[j]["median_ipc"] = (solo_ipcA[j] + solo_ipcB[j]) / 2.0

    # Step 2: Measure all 45 pairs on (cpuA, cpuB)
    pair_scores = {}
    pair_details = {}

    for i in range(NUM_JOBS):
        for j in range(i + 1, NUM_JOBS):
            scores = []
            ipcs_i = []
            ipcs_j = []
            for r in range(PAIR_REPEATS):
                # Alternate placement on cpuA/cpuB to eliminate any slight thread asymmetry
                if r % 2 == 0:
                    ipc_i, ipc_j = run_pair_measurement(
                        serial_no, i, j, cpuA, cpuB, WARMUP_ITERS, MEAS_ITERS
                    )
                    base_i = solo_ipcA[i] if solo_ipcA[i] > 0 else 1.0
                    base_j = solo_ipcB[j] if solo_ipcB[j] > 0 else 1.0
                else:
                    ipc_j, ipc_i = run_pair_measurement(
                        serial_no, j, i, cpuA, cpuB, WARMUP_ITERS, MEAS_ITERS
                    )
                    base_i = solo_ipcB[i] if solo_ipcB[i] > 0 else 1.0
                    base_j = solo_ipcA[j] if solo_ipcA[j] > 0 else 1.0

                s = (ipc_i / base_i) + (ipc_j / base_j)
                scores.append(s)
                ipcs_i.append(ipc_i)
                ipcs_j.append(ipc_j)

            med_score = statistics.median(scores)
            pair_scores[(i, j)] = med_score
            pair_scores[(j, i)] = med_score
            pair_details[(i, j)] = {
                "ipcA_alone": solo_stats[i]["median_ipc"],
                "ipcB_alone": solo_stats[j]["median_ipc"],
                "ipcA_paired": statistics.median(ipcs_i),
                "ipcB_paired": statistics.median(ipcs_j),
                "score": med_score,
            }

    # Step 3: Find global optimal schedule via exact bitmask DP over all 945 matchings
    best_avg_score, best_pairs = find_optimal_matching(pair_scores)

    # Write final schedule.txt (strictly 5 lines of "A B")
    with open("schedule.txt", "w") as f:
        for a, b in best_pairs:
            f.write(f"{a} {b}\n")

    # Save a detailed summary file (`profile_report_data.txt`) so you have all
    # exact numbers for report.pdf without polluting stdout during grading.
    with open("profile_report_data.txt", "w") as f:
        f.write(f"SERIAL_NO: {serial_no}\n")
        f.write(f"SIBLING_CORES: {cpuA},{cpuB}\n\n")
        f.write("=== STEP 1: SOLO JOB CLASSIFICATION DATA ===\n")
        f.write(f"{'Slot':<5} {'IPC':<10} {'Inst':<14} {'Cycles':<14} {'CacheRefs':<12} {'CacheMiss':<12} {'MPKI':<10} {'Class'}\n")
        for j in range(NUM_JOBS):
            st = solo_stats[j]
            inst = max(st["inst"], 1)
            cmis = st["cmis"]
            mpki = (cmis * 1000.0) / inst
            # Compute-bound jobs have high IPC (>1.2) and very low cache MPKI (<1.0);
            # Memory-bound jobs have lower IPC and significant cache traffic/misses.
            jclass = "compute-bound" if (st["median_ipc"] > 1.15 and mpki < 1.0) else "memory-bound"
            f.write(f"{j:<5} {st['median_ipc']:<10.4f} {st['inst']:<14} {st['cycles']:<14} {st['cref']:<12} {st['cmis']:<12} {mpki:<10.3f} {jclass}\n")

        f.write("\n=== STEP 2 & 3: OPTIMAL SCHEDULE TABLE ===\n")
        f.write(f"{'Pair':<5} {'SlotA':<6} {'SlotB':<6} {'IPC_A_alone':<12} {'IPC_B_alone':<12} {'IPC_A_pair':<12} {'IPC_B_pair':<12} {'PairScore':<10}\n")
        for idx, (a, b) in enumerate(best_pairs, 1):
            d = pair_details[(a, b)]
            f.write(f"{idx:<5} {a:<6} {b:<6} {d['ipcA_alone']:<12.4f} {d['ipcB_alone']:<12.4f} {d['ipcA_paired']:<12.4f} {d['ipcB_paired']:<12.4f} {d['score']:<10.4f}\n")
        f.write(f"Average Schedule Score: {best_avg_score:.4f}\n\n")

        f.write("=== ALL 45 PAIR SCORES ===\n")
        for (a, b), d in sorted(pair_details.items(), key=lambda x: x[1]["score"], reverse=True):
            f.write(f"({a}, {b}): score={d['score']:.4f} (A: {d['ipcA_alone']:.3f}->{d['ipcA_paired']:.3f}, B: {d['ipcB_alone']:.3f}->{d['ipcB_paired']:.3f})\n")


if __name__ == "__main__":
    main()