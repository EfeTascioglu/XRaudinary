# Server-Side Desynchronization Analysis (app.py)

## Critical Issues Found

### 1. **Three Independent UDP Receiver Threads** (HIGH IMPACT)
**Location:** `udp_receiver()` function spawned 3 times for each mic
**Problem:** 
- Each thread runs independently with its own timing
- Thread scheduling varies based on OS and system load
- MIC0 thread might process packet N while MIC1 thread is still receiving packet N-1
- No barrier or synchronization between the three threads

**Impact:** ±100-500ms temporal drift between microphones

```python
# CURRENT: Independent threads, no coordination
Thread_MIC0: [Recv] → [Validate] → [Lock+Buffer] → [RMS] → [Timestamp] → [Subscribe]
Thread_MIC1: [Recv] → [Validate] → [Lock+Buffer] → [RMS] → [Timestamp] → [Subscribe]
Thread_MIC2: [Recv] → [Validate] → [Lock+Buffer] → [RMS] → [Timestamp] → [Subscribe]
         ↑ These can be out of sync!
```

### 2. **Variable-Duration Operations Outside Lock** (MEDIUM IMPACT)
**Location:** Lines 210-238 in `udp_receiver()`
**Problem:**
- RMS computation duration varies (CPU-dependent)
- Base64 encoding duration varies (CPU-dependent)
- These operations happen BEFORE lock acquisition
- Other threads may write during this time, causing interleaving

```python
# Line 211-212: Variable duration (depends on sample content)
rms_values = _compute_rms(data, 1, 32)  # ← CPU-intensive, duration varies!
data_b64 = base64.b64encode(data).decode('utf-8')  # ← Also variable duration

# Meanwhile, another thread might write to the buffer
```

**Impact:** ±10-50ms skew between threads during heavy processing

### 3. **Timestamps Generated Per-Thread, Not Synchronized** (MEDIUM IMPACT)
**Location:** Line 221
**Problem:**
- Each thread generates its own timestamp independently
- Timestamps use `time.strftime()` which is called at different times
- Even though packets from the same ESP32 cycle arrive microseconds apart, timestamps differ by milliseconds

```python
# Thread_MIC0 timestamp: "2026-02-17 14:30:00" (generated first)
# Thread_MIC1 timestamp: "2026-02-17 14:30:00" (generated 5ms later)
# Thread_MIC2 timestamp: "2026-02-17 14:30:00" (generated 10ms later)
# ↑ Metadata shows they're different, but timestamps are identical (1-second precision)
```

**Impact:** Loses millisecond-level timing information

### 4. **Lock Contention on `audio_buffer_lock`** (MEDIUM-LOW IMPACT)
**Location:** Line 199 - three threads competing for same lock
**Problem:**
- All three receiver threads acquire the same lock to write to buffers
- Write times can vary significantly
- If one thread's lock hold is delayed, buffer writes become uneven

```python
with audio_buffer_lock:  # ← All 3 threads compete here
    audio_buffer.append(data)
    if mic_id == 0: audio_buffer_mic0.append(data)
    elif mic_id == 1: audio_buffer_mic1.append(data)
    else: audio_buffer_mic2.append(data)
```

**Impact:** ±5-20ms skew due to lock scheduling variance

### 5. **`/api/stream_3ch` Waits for All Buffers to Have New Data** (HIGH IMPACT for streaming)
**Location:** Lines 954-955
**Problem:**
- Gate: `if mic0_len > last_mic0_len and mic1_len > last_mic1_len and mic2_len > last_mic2_len:`
- If MIC2 is slow, output stalls until all three catch up
- Creates artificial synchronization barriers that introduce latency variance

```python
# If MIC0 and MIC1 have new data but MIC2 is 50ms behind:
# → Output waits 50ms for MIC2
# → Creates jitter in stream timing
```

**Impact:** ±50-200ms streaming latency variation

### 6. **Padding with Zeros Masks Timing Misalignment** (MEDIUM IMPACT)
**Location:** Lines 978-980
**Problem:**
- Uses `np.pad()` to fill missing samples with zeros
- This hides the fact that channels are unequal length
- Client has no way to know the temporal offset between channels

```python
# If mic0_samples has 512 samples and mic1_samples has 510:
mic0_samples = np.pad(mic0_samples, (0, max_len - len(mic0_samples)), mode='constant')
# ↑ Adds 2 zero samples at END instead of tracking the offset
```

**Impact:** Timing information lost; client can't correct misalignment

### 7. **No Packet Grouping or Numbering** (LOW-MEDIUM IMPACT)
**Location:** Throughout receiver
**Problem:**
- No way to verify packets come from the same capture cycle on ESP32
- If a packet is dropped, client doesn't know which channel it came from
- Buffer sizes can drift apart indefinitely
- No "packet group ID" to link related packets

**Impact:** Progressive desynchronization over time; impossible to detect

### 8. **Subscriber Queue Updates Not Atomic with Buffer Writes** (LOW-MEDIUM IMPACT)
**Location:** Lines 225-237
**Problem:**
- Packets added to `audio_buffer_mic*` (inside lock at line 199)
- Packets added to subscriber queues (outside lock at line 237)
- These operations not synchronized
- Clients might see queue events out of order relative to buffer state

**Impact:** Client-side reassembly gets confused timing

---

## Severity Ranking

| Issue | Severity | Typical Offset |
|-------|----------|-----------------|
| Independent threads | **HIGH** | ±100-500ms |
| Stream wait-for-all gate | **HIGH** | ±50-200ms |
| Variable RMS/encoding time | **MEDIUM** | ±10-50ms |
| Lock contention | **MEDIUM-LOW** | ±5-20ms |
| Per-thread timestamps | **MEDIUM** | Loss of precision |
| Padding masks offset | **MEDIUM** | Unknown offset |
| No packet grouping | **MEDIUM** | Progressive drift |
| Queue ordering | **LOW-MEDIUM** | Occasional jitter |

**Total worst-case offset: ±165-770ms**

---

## Recommendations

### Immediate (High Priority)
1. **Remove the "all buffers have new data" gate** - Stream as soon as any buffer updates
2. **Add packet group IDs** - Link packets from same ESP32 cycle
3. **Track actual buffer length differences** - Report offsets instead of padding
4. **Measure thread timing skew** - Add diagnostics

### Medium Priority
5. **Synchronize timestamps** - Generate single shared timestamp for all three packets
6. **Move RMS/encoding outside lock precedence** - Pre-compute before locking
7. **Add explicit sync markers** - Include ESP32-generated timestamps in packets

### Long-term
8. **Consider single multiplexed UDP receiver** - Single thread with per-port selection might reduce jitter
9. **Track per-mic offsets** - Measure and report delays relative to MIC0
10. **Add buffer synchronization protocol** - Ensure packets arrive as matched groups
