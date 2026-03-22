#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <cmath>
#include "driver/i2s.h"

// DMA I2S
// Access second core as well. 

// WiFi settings
static const char *WIFI_SSID = "Virus Network";
static const char *WIFI_PASS = "12345678";

// Server settings
static const char *DEVICE_ID = "esp32-nauthiz-01";
// static const char *SERVER_IP = "10.213.87.70"; // ANDY'S COMPUTER
static const char *SERVER_IP = "10.213.87.53"; // MATTHEW'S COMPUTER
static const int UDP_PORT_COMBINED = 30001;  // Single port for all 3 channels combined

// UDP client (single socket for combined transmission)
static WiFiUDP udp_combined;

// I2S pins (set these to match your wiring)
static const int I2S0_BCLK = 26;
static const int I2S0_WS = 25;
static const int I2S0_DATA_IN = 33;

static const int I2S1_BCLK = 15; // 15
static const int I2S1_WS = 13; // 13
static const int I2S1_DATA_IN = 34;

// Audio settings
// DATA FORMAT: 24 bit, 2's complement, MSB First. 
static const int SAMPLE_RATE_HZ = 48000;
static const int I2S_BITS_PER_SAMPLE = 32; 
static const int PCM_BITS_PER_SAMPLE = 32; 
static const int I2S_SAMPLE_SHIFT = 0; // 18-bit left-justified -> right-align into 32-bit
static const size_t FRAMES_PER_CHUNK = 512; 

// Buffers
static int32_t i2s0_raw[FRAMES_PER_CHUNK * 2];
static int32_t i2s1_raw[FRAMES_PER_CHUNK];
static int32_t pcm_out[FRAMES_PER_CHUNK * 3];
static int32_t i2s0_mono[FRAMES_PER_CHUNK];  // Mono channel extraction buffer (moved from stack to avoid overflow)
static int32_t i2s1_mono[FRAMES_PER_CHUNK];  // Mono channel extraction buffer (moved from stack to avoid overflow)
static int32_t discard_buffer[FRAMES_PER_CHUNK];
// TODO: To optimize we can ignore these entirely and just pull from raw.

// Diagnostics
static bool ENABLE_TIMING_DIAGNOSTICS = true;  // Toggle this to enable/disable timing tracking
static unsigned long last_diagnostic_time = 0;

// Test Mode Configuration
static bool ENABLE_TEST_MODE = false;  // Set to true to send test waveforms instead of microphone input
static const float TEST_FREQUENCY_HZ = 440.0f;  // Test signal frequency (sine, square, triangle)
static const float TEST_AMPLITUDE = 131072.0f;  // Amplitude scale for 32-bit samples
static size_t test_phase_index = 0;  // Global phase tracker for synchronized waveforms
static unsigned long total_frames_captured = 0;
static unsigned long total_bytes_uploaded = 0;
static unsigned int chunk_count = 0;
static unsigned long max_upload_time_ms = 0;
static unsigned long min_upload_time_ms = ULONG_MAX;
static unsigned long total_i2s_read_time = 0;
static unsigned long total_convert_time = 0;
static unsigned long total_loop_time = 0;
static unsigned long last_i2s0_read_time = 0;  // Track time of last successful I2S0 read
static unsigned long i2s0_read_interval_ms = 0;  // Time between successful I2S0 reads

// Manual Offset Calibration
static int i2s1_sample_offset = 0; // Sample offset for I2S1 channel to align with I2S0
static volatile bool calibration_complete = false;  // Flag indicating calibration done

// Manual Calibration Mode
static volatile bool manual_calibration_mode = false;  // Flag to enter manual calibration mode (stops transmission)
static volatile int manual_discard_i2s0_samples = 0;   // Number of samples to discard from I2S0 buffer
static volatile int manual_discard_i2s1_samples = 0;   // Number of samples to discard from I2S1 buffer
static volatile int manual_buffer_offset_adjustment = 0;  // Additional offset to apply (can be negative)

// Calibration parameters
static int CLAP_THRESHOLD = 21000000;  // Threshold for detecting loud peaks (will be set dynamically based on baseline)
static const int MIN_CLAP_SPACING = 1500 ;  // ms
static const int TRACKING_WINDOW_MS = 200;
static const int NUM_CLAPS_FOR_CALIBRATION = 3;  // Use 3-5 claps for averaging
static const int MAX_OFFSET_SAMPLES = SAMPLE_RATE_HZ / 10;  // Max expected offset: 50ms
static const float CLAP_THRESHOLD_MULTIPLIER = 1.5f;  // Threshold is 1.5x baseline ambient volume
static const unsigned long BASELINE_MEASUREMENT_TIME = 2000;  // Measure 2 seconds of ambient noise for baseline
static int baseline_amplitude = 0;  // Measured baseline for diagnostics 

// Packet transmission tracking (single atomic transmission)
static unsigned int packets_sent_combined = 0;   // Successfully sent combined 3-channel packet
static unsigned int packets_failed_combined = 0; // Failed to send combined packet
static unsigned int sync_perfect = 0;             // Counter for sample-perfect alignment achieved

// Timing index: encoded in bits 3-5 to mark which packet frames belong to
// Increments with each successful transmission, wraps 0-7 (3 bits)
// Allows server to validate 3 consecutive frames came from same synchronized I2S read
static uint8_t timing_index = 0;

static inline int32_t convert_sample(int32_t raw) {
  return raw >> I2S_SAMPLE_SHIFT;
}

// Channel identification tagging: encode channel ID into bits 1-2 of each sample
// This allows the server to detect channel misalignment without separate headers
// Bits 1-2 are used for identification: 00=channel0, 01=channel1, 10=channel2, 11=reserved
static inline int32_t tag_sample_with_channel(int32_t sample, uint8_t channel) {
  // Clear bits 1-2, then set them to channel ID
  int32_t mask = sample & ~0x6;  // 0x6 = 0b110 (bits 1-2)
  return mask | ((channel & 0x3) << 1);
}

// Extract channel tag from sample bits 1-2
static inline uint8_t extract_channel_tag(int32_t sample) {
  return (sample >> 1) & 0x3;
}

// Extract timing index from sample bits 3-5
static inline uint8_t extract_timing_index(int32_t sample) {
  return (sample >> 3) & 0x7;
}

// Tag sample with both channel ID (bits 1-2) and timing index (bits 3-5)
// Timing index marks which packet/cluster this frame belongs to
// Server validates 3 consecutive frames have matching timing index (same packet sync)
static inline int32_t tag_sample_with_timing_and_channel(int32_t sample, uint8_t channel, uint8_t timing_idx) {
  // Clear bits 1-2, then set them to channel ID
  sample = (sample & ~0x6) | ((channel & 0x3) << 1);
  // Clear bits 3-5, then set them to timing index
  sample = (sample & ~0x38) | (((uint32_t)timing_idx & 0x7) << 3);
  return sample;
}

// Test Mode Wave Generation Functions
// Generate sine wave sample at phase index
static inline int32_t generate_sine_sample(size_t phase_idx) {
  float t = (float)phase_idx / SAMPLE_RATE_HZ;
  float sample = TEST_AMPLITUDE * sinf(2.0f * M_PI * TEST_FREQUENCY_HZ * t);
  
  // Convert to 18-bit signed (2's complement): range -131072 to +131071
  int32_t sample_18bit = (int32_t)sample;
  // Clamp to 18-bit signed range
  if (sample_18bit > 131071) sample_18bit = 131071;
  if (sample_18bit < -131072) sample_18bit = -131072;
  
  // Left-shift by 14 bits: (18 bits of signal)(14 bits of 0s)
  int32_t shifted_sample = sample_18bit << 14;
  
  // Serial.printf(">SINESAMPLE:0x%08X", shifted_sample); // Debug print in hex
  // Serial.println();
  return shifted_sample;
}


// Generate triangle wave sample at phase index
static inline int32_t generate_triangle_sample(size_t phase_idx) {
  float t = (float)phase_idx / SAMPLE_RATE_HZ;
  float phase_normalized = fmodf(TEST_FREQUENCY_HZ * t, 1.0f);  // 0.0 to 1.0
  float sample;
  if (phase_normalized < 0.25f) {
    // Rising from -1 to 1: first quarter
    sample = TEST_AMPLITUDE * (-1.0f + 4.0f * phase_normalized);
  } else if (phase_normalized < 0.75f) {
    // Falling from 1 to -1: middle half
    sample = TEST_AMPLITUDE * (1.0f - 4.0f * (phase_normalized - 0.25f));
  } else {
    // Rising from -1 to 1: last quarter
    sample = TEST_AMPLITUDE * (-1.0f + 4.0f * (phase_normalized - 0.75f));
  }
  
  // Convert to 18-bit signed (2's complement): range -131072 to +131071
  int32_t sample_18bit = (int32_t)sample;
  // Clamp to 18-bit signed range
  if (sample_18bit > 131071) sample_18bit = 131071;
  if (sample_18bit < -131072) sample_18bit = -131072;
  
  // Left-shift by 14 bits: (18 bits of signal)(14 bits of 0s)
  int32_t shifted_sample = sample_18bit << 14;
  
  // Serial.printf(">TRIANGLESAMPLE:0x%08X", shifted_sample); // Debug print in hex
  // Serial.println();
  return shifted_sample;
}

static void setup_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("Connecting to WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi connected, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi connect failed");
  }
}

static void setup_i2s_port(i2s_port_t port, const i2s_pin_config_t &pins, i2s_channel_fmt_t chan_fmt, i2s_channel_t channel_count, i2s_mode_t mode) {
  i2s_config_t config = {};
  config.mode = static_cast<i2s_mode_t>(mode | I2S_MODE_RX); // Always enable RX mode for reading from microphones
  config.sample_rate = SAMPLE_RATE_HZ;
  config.bits_per_sample = static_cast<i2s_bits_per_sample_t>(I2S_BITS_PER_SAMPLE);
  config.channel_format = chan_fmt;
  config.communication_format = static_cast<i2s_comm_format_t>(I2S_COMM_FORMAT_I2S);
  config.intr_alloc_flags = 0;
  config.dma_buf_count = 8;        // Increased from 6 for better buffering
  config.dma_buf_len = FRAMES_PER_CHUNK; 
  config.use_apll = false;          // Enable APLL for better clock accuracy (changed from false)
  config.tx_desc_auto_clear = false; // Auto clear tx descriptor on underflow (helps prevent noise)
  config.fixed_mclk = 0; // Let IDF manage MCLK when master, fix MCLK when slave for better sync

  Serial.printf("Setting up I2S port %d...\n", port);

  i2s_driver_install(port, &config, 0, nullptr);
  Serial.printf("I2S port %d driver installed.\n", port);
  i2s_set_pin(port, &pins);
  Serial.printf("I2S port %d pins set.\n", port);
}

static void setup_i2s() {
  i2s_pin_config_t pins0 = {};
  pins0.bck_io_num = I2S0_BCLK;
  pins0.ws_io_num = I2S0_WS;
  pins0.data_out_num = I2S_PIN_NO_CHANGE;
  pins0.data_in_num = I2S0_DATA_IN;

  i2s_pin_config_t pins1 = {};
  pins1.bck_io_num = I2S1_BCLK;
  pins1.ws_io_num = I2S1_WS;
  pins1.data_out_num = I2S_PIN_NO_CHANGE;
  pins1.data_in_num = I2S1_DATA_IN;

  Serial.println("Initializing I2S...");

  setup_i2s_port(I2S_NUM_0, pins0, I2S_CHANNEL_FMT_RIGHT_LEFT, I2S_CHANNEL_STEREO, I2S_MODE_MASTER);
  setup_i2s_port(I2S_NUM_1, pins1, I2S_CHANNEL_FMT_ONLY_LEFT, I2S_CHANNEL_MONO, I2S_MODE_MASTER);
  i2s_set_clk(I2S_NUM_0, SAMPLE_RATE_HZ, static_cast<i2s_bits_per_sample_t>(I2S_BITS_PER_SAMPLE), I2S_CHANNEL_STEREO);
  i2s_set_clk(I2S_NUM_1, SAMPLE_RATE_HZ, static_cast<i2s_bits_per_sample_t>(I2S_BITS_PER_SAMPLE), I2S_CHANNEL_MONO);
  Serial.printf("I2S ports clock set.\n");
}

static bool send_udp_combined(WiFiUDP &udp, int port, const int32_t *data_3ch, size_t frames) {
  if (WiFi.status() != WL_CONNECTED) {
    if (ENABLE_TIMING_DIAGNOSTICS) Serial.println("[UDP] WiFi not connected");
    return false;
  }

  // Combined packet: all 3 channels interleaved [L, R, C, L, R, C, ...]
  size_t bytes = frames * 3 * sizeof(int32_t);
  
  unsigned long send_start = 0;
  if (ENABLE_TIMING_DIAGNOSTICS) {
    send_start = millis();
  }

  // Send single atomic packet with all 3 channels
  int result = 0;
  if (udp.beginPacket(SERVER_IP, port)) {
    size_t written = udp.write(reinterpret_cast<const uint8_t *>(data_3ch), bytes);
    result = udp.endPacket();
    
    if (ENABLE_TIMING_DIAGNOSTICS && written != bytes) {
      Serial.printf("[UDP] Write mismatch: wrote %d/%lu bytes (3 channels, %lu frames)\n", written, bytes, frames);
    }
  } else {
    if (ENABLE_TIMING_DIAGNOSTICS) {
      Serial.printf("[UDP] beginPacket() failed for port %d\n", port);
    }
    result = 0;
  }

  bool success = (result == 1);
  // printf("Sent: %s %d bytes (3 channels, %lu frames) in one packet\n", success ? "SUCCESS" : "FAILURE", bytes, frames);
  
  // Track packet statistics
  if (success) {
    packets_sent_combined++;
    sync_perfect++;  // Since all 3 channels sent in one packet, sync is perfect
    total_bytes_uploaded += bytes;
    
    // Note: timing_index is now incremented inside the frame assembly loop (per-frame)
    // rather than per-packet, so we don't increment it here
  } else {
    packets_failed_combined++;
  }

  if (ENABLE_TIMING_DIAGNOSTICS) {
    unsigned long send_time = millis() - send_start;
    max_upload_time_ms = max(max_upload_time_ms, send_time);
    min_upload_time_ms = min(min_upload_time_ms, send_time);
  }

  return success;
}

void send_test_chunk() {
  // Generate 3-channel interleaved sine wave test: 440Hz
  const float frequency = 440.0f;
  const float amplitude = 65536.0f; // scale for 32-bit
  int32_t test_data[FRAMES_PER_CHUNK * 3] = {};

  for (size_t i = 0; i < FRAMES_PER_CHUNK; ++i) {
    float t = (float)i / SAMPLE_RATE_HZ;
    float sample = amplitude * sinf(2.0f * M_PI * frequency * t);
    int32_t s = (int32_t)sample;
    test_data[i * 3] = s;       // Left
    test_data[i * 3 + 1] = s;   // Right
    test_data[i * 3 + 2] = s;   // Center
  }

  send_udp_combined(udp_combined, UDP_PORT_COMBINED, test_data, FRAMES_PER_CHUNK);
}

void send_audio_to_serial() {
  for (size_t i = 0; i < FRAMES_PER_CHUNK * 3; i += 3) {
    // Print in hex for debugging; in practice, you might want to print in hex or just the raw bytes
    Serial.write(reinterpret_cast<const uint8_t *>(&pcm_out[i]), sizeof(int32_t) * 3);
    // Serial.println(pcm_out[i]);
    if (i >= 21) { // Limit how much we print to serial for debugging
      break;
    }
  }
}

// Find peak in a buffer by looking for max absolute value
static size_t find_peak_index(const int32_t *buffer, size_t len) {
  size_t peak_idx = 0;
  int32_t peak_val = 0;
  for (size_t i = 0; i < len; ++i) {
    int32_t abs_val = buffer[i] > 0 ? buffer[i] : -buffer[i];
    if (abs_val > peak_val) {
      peak_val = abs_val;
      peak_idx = i;
    }
  }
  return peak_idx;
}

// Measure baseline ambient volume to establish dynamic clap threshold
static int measure_baseline_volume() {
  Serial.println("\n=== MEASURING BASELINE VOLUME ===");
  Serial.println("Measuring ambient noise for 2 seconds... Keep quiet!");
  
  int32_t max_amplitude = 0;
  unsigned long baseline_start = millis();
  unsigned long last_readout = baseline_start;
  
  while (millis() - baseline_start < BASELINE_MEASUREMENT_TIME) {
    size_t bytes_read0 = 0;
    size_t bytes_read1 = 0;
    int portTICKDELAY = 50;  // 50ms timeout for reads

    // Read both I2S ports
    i2s_read(I2S_NUM_0, i2s0_raw, sizeof(i2s0_raw), &bytes_read0, portTICKDELAY);
    i2s_read(I2S_NUM_1, i2s1_raw, sizeof(i2s1_raw), &bytes_read1, portTICKDELAY);

    if (bytes_read0 == 0 || bytes_read1 == 0) {
      delay(5);
      continue;
    }

    // Calculate frames
    size_t frames0 = bytes_read0 / (sizeof(int32_t) * 2);
    size_t frames1 = bytes_read1 / (sizeof(int32_t) * 2);
    size_t frames = frames0 < frames1 ? frames0 : frames1;

    // Find max amplitude in both channels
    for (size_t i = 0; i < frames; ++i) {
      int32_t sample0 = i2s0_raw[i * 2];  // Left channel of I2S0
      int32_t sample1 = i2s1_raw[i];  // Left channel of I2S1
      
      int32_t abs_sample0 = sample0 > 0 ? sample0 : -sample0;
      int32_t abs_sample1 = sample1 > 0 ? sample1 : -sample1;
      
      if (abs_sample0 > max_amplitude) max_amplitude = abs_sample0;
      if (abs_sample1 > max_amplitude) max_amplitude = abs_sample1;
    }
    
    // Print live volume readout every 500ms
    unsigned long now = millis();
    if (now - last_readout >= 500) {
      float elapsed_pct = (float)(now - baseline_start) / BASELINE_MEASUREMENT_TIME * 100.0f;
      Serial.printf("[%.0f%%] Current max: %d\n", elapsed_pct, max_amplitude);
      last_readout = now;
    }
    
    delay(5);
  }
  
  Serial.printf("Baseline max amplitude: %d\n", max_amplitude);
  return max_amplitude;
}


// Handle serial commands for manual calibration
static void handle_serial_command() {
  // If not in calibration mode, only process if data is explicitly available (non-blocking)
  if (!manual_calibration_mode && Serial.available() == 0) {
    return;
  }
  
  // Even in calibration mode, check if data is available (Arduino doesn't support true blocking)
  if (Serial.available() == 0) {
    return;
  }
  
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  
  if (cmd.length() == 0) {
    return;
  }
  
  // Parse command format: COMMAND [arg1] [arg2]
  if (cmd.equals("CAL")) {
    // Enter manual calibration mode
    manual_calibration_mode = true;
    manual_discard_i2s0_samples = 0;
    manual_discard_i2s1_samples = 0;
    manual_buffer_offset_adjustment = 0;
    Serial.println("[MANUAL_CAL] Entering manual calibration mode. Transmission PAUSED.");
    Serial.println("[MANUAL_CAL] Available commands:");
    Serial.println("[MANUAL_CAL]   D0 <samples>  - Discard N samples from I2S0 buffer");
    Serial.println("[MANUAL_CAL]   D1 <samples>  - Discard N samples from I2S1 buffer");
    Serial.println("[MANUAL_CAL]   TEST          - Send one chunk and exit calibration to test alignment");
    Serial.println("[MANUAL_CAL]   OK            - Apply settings and resume streaming");
    Serial.println("[MANUAL_CAL]   CANCEL        - Discard changes and resume streaming");
    Serial.println("[MANUAL_CAL]   STATUS        - Show current calibration settings");
  }
  else if (manual_calibration_mode) {
    // Process calibration commands
    if (cmd.startsWith("D0")) {
      int samples = cmd.substring(2).toInt();
      manual_discard_i2s0_samples = samples;
      Serial.printf("[MANUAL_CAL] I2S0 discard amount set to %d samples (%.3f ms)\n", 
                    samples, (float)samples * 1000.0 / SAMPLE_RATE_HZ);
    }
    else if (cmd.startsWith("D1")) {
      int samples = cmd.substring(2).toInt();
      manual_discard_i2s1_samples = samples;
      Serial.printf("[MANUAL_CAL] I2S1 discard amount set to %d samples (%.3f ms)\n", 
                    samples, (float)samples * 1000.0 / SAMPLE_RATE_HZ);
    }
    else if (cmd.equals("TEST")) {
      // Temporarily exit calibration to send one test chunk
      Serial.println("[MANUAL_CAL] Sending test chunk with current settings...");
      manual_calibration_mode = false;
      delay(100);  // Allow one loop iteration to send
      delay(100);  // Wait a bit
      manual_calibration_mode = true;
      Serial.println("[MANUAL_CAL] Test chunk sent. Resume calibration mode.");
    }
    else if (cmd.equals("OK")) {
      // Apply settings and resume normal streaming
      int bytes_to_discard = (manual_discard_i2s0_samples > 0) ? 
                      min((size_t)manual_discard_i2s0_samples * sizeof(int32_t), sizeof(discard_buffer)) : 0; 
      size_t bytes_read = 0;
      if (manual_discard_i2s0_samples > 0){
        i2s_read(I2S_NUM_0, discard_buffer, bytes_to_discard, &bytes_read, 1000);
        int samples_discarded = bytes_read / sizeof(int32_t);
      }
      else if (manual_discard_i2s1_samples > 0){
        i2s_read(I2S_NUM_1, discard_buffer, bytes_to_discard, &bytes_read, 1000);
        int samples_discarded = bytes_read / sizeof(int32_t);
      }
      Serial.println("[MANUAL_CAL] Settings applied!");
      Serial.printf("[MANUAL_CAL] Final offset: I2S0=%d samples, I2S1=%d samples, Net offset=%d\n",
                    manual_discard_i2s0_samples, manual_discard_i2s1_samples, i2s1_sample_offset);
      manual_calibration_mode = false;
      Serial.println("[MANUAL_CAL] Resuming normal audio streaming.");
    }
    else if (cmd.equals("CANCEL")) {
      // Exit without applying changes
      manual_calibration_mode = false;
      manual_discard_i2s0_samples = 0;
      manual_discard_i2s1_samples = 0;
      Serial.println("[MANUAL_CAL] Cancelled. Resuming normal audio streaming.");
    }
    else if (cmd.equals("STATUS")) {
      // Show current settings
      Serial.println("[MANUAL_CAL] === Current Settings ===");
      Serial.printf("[MANUAL_CAL] I2S0 discard: %d samples (%.3f ms)\n", 
                    manual_discard_i2s0_samples, (float)manual_discard_i2s0_samples * 1000.0 / SAMPLE_RATE_HZ);
      Serial.printf("[MANUAL_CAL] I2S1 discard: %d samples (%.3f ms)\n", 
                    manual_discard_i2s1_samples, (float)manual_discard_i2s1_samples * 1000.0 / SAMPLE_RATE_HZ);
      Serial.printf("[MANUAL_CAL] Resulting offset: %d samples\n", 
                    manual_discard_i2s1_samples - manual_discard_i2s0_samples);
    }
    else {
      Serial.println("[MANUAL_CAL] Unknown command. Type 'STATUS' to see current settings.");
    }
  }
  else {
    // Not in manual calibration mode - other commands can go here
    if (cmd.equals("CAL")) {
      // Already handled above
    }
    else {
      Serial.printf("[UNKNOWN] Unknown command: %s\n", cmd.c_str());
    }
  }
}
// Calibration sequence: listen for claps on startup and measure timing drift
static void run_calibration() {
  Serial.println("\n=== STARTING CALIBRATION SEQUENCE ===");
  
  // Step 1: Measure baseline volume
  baseline_amplitude = measure_baseline_volume();
  
  // Step 2: Set clap threshold to 1.5x baseline
  CLAP_THRESHOLD = (int)((float)baseline_amplitude );
  Serial.printf("Clap threshold set to: %d\n", CLAP_THRESHOLD);
  
  delay(1000);
  Serial.println("\nPlease clap 3-5 times near the microphone to calibrate sample alignment...");
  delay(1000);

  // Store peak positions across both I2S channels for multiple claps
  int peak_offsets[NUM_CLAPS_FOR_CALIBRATION] = {0};
  int clap_count = 0;
  unsigned long last_peak_time = 0;
  unsigned long calibration_start = millis();
  const unsigned long CALIBRATION_TIMEOUT = 60000;  // 30 second timeout

  // Track loudest peaks across multiple buffers during clap detection
  int32_t i2s0_loudest_val = 0;
  long i2s0_loudest_index = 0;
  int32_t i2s1_loudest_val = 0;
  long i2s1_loudest_index = 0;
  size_t buffer_count = 0;
  bool tracking_clap = false;
  unsigned long tracking_start = 0;

  while (clap_count < NUM_CLAPS_FOR_CALIBRATION) {
    if (millis() - calibration_start > CALIBRATION_TIMEOUT) {
      Serial.println("Calibration timeout. Using default offset of 0.");
      i2s1_sample_offset = 0;
      calibration_complete = true;
      return;
    }

    size_t bytes_read0 = 0;
    size_t bytes_read1 = 0;
    int portTICKDELAY = 100;  // 100ms timeout for reads

    // Read both I2S ports
    i2s_read(I2S_NUM_0, i2s0_raw, sizeof(i2s0_raw), &bytes_read0, portTICKDELAY);
    i2s_read(I2S_NUM_1, i2s1_raw, sizeof(i2s1_raw), &bytes_read1, portTICKDELAY);

    if (bytes_read0 == 0 || bytes_read1 == 0) {
      delay(5);
      continue;
    }

    size_t frames = FRAMES_PER_CHUNK;

    // Extract channels and find peaks (using global buffers to avoid stack overflow)
    for (size_t i = 0; i < frames; ++i) {
      i2s0_mono[i] = i2s0_raw[i * 2];  // Left channel of I2S0
      i2s1_mono[i] = i2s1_raw[i];  // Left channel of I2S1
    }

    // Find peaks in this buffer
    size_t peak0_idx = find_peak_index(i2s0_mono, frames);
    size_t peak1_idx = find_peak_index(i2s1_mono, frames);
    int32_t peak0_val = i2s0_mono[peak0_idx] > 0 ? i2s0_mono[peak0_idx] : -i2s0_mono[peak0_idx];
    int32_t peak1_val = i2s1_mono[peak1_idx] > 0 ? i2s1_mono[peak1_idx] : -i2s1_mono[peak1_idx];

    // Check if we should start tracking a new clap
    if (!tracking_clap && (peak0_val > CLAP_THRESHOLD || peak1_val > CLAP_THRESHOLD) &&
        (millis() - last_peak_time > MIN_CLAP_SPACING)) {
      // Start tracking this clap across buffers
      tracking_clap = true;
      tracking_start = millis();
      buffer_count = 0;
      
      // Initialize with current buffer's peaks
      i2s0_loudest_val = peak0_val;
      i2s0_loudest_index = peak0_idx;
      i2s1_loudest_val = peak1_val;
      i2s1_loudest_index = peak1_idx;
      
      Serial.printf("*** CLAP %d TRACKING STARTED ***\n", clap_count + 1);
      Serial.printf("Initial peaks: I2S0=%d @%ld, I2S1=%d @%ld\n", 
                    peak0_val, i2s0_loudest_index, peak1_val, i2s1_loudest_index);
    }
    
    // If we're tracking a clap, update loudest peaks if current buffer has louder ones
    if (tracking_clap) {
      buffer_count++;
      
      // Update I2S0 loudest if this buffer has a louder peak
      if (peak0_val > i2s0_loudest_val) {
        i2s0_loudest_val = peak0_val;
        i2s0_loudest_index = buffer_count * FRAMES_PER_CHUNK + peak0_idx;
        Serial.printf("  New I2S0 peak: %d @%ld\n", peak0_val, i2s0_loudest_index);
      }
      
      // Update I2S1 loudest if this buffer has a louder peak
      if (peak1_val > i2s1_loudest_val) {
        i2s1_loudest_val = peak1_val;
        i2s1_loudest_index = buffer_count * FRAMES_PER_CHUNK + peak1_idx;
        Serial.printf("  New I2S1 peak: %d @%ld\n", peak1_val, i2s1_loudest_index);
      }

      if (millis() - tracking_start > TRACKING_WINDOW_MS) {
        // Calculate offset using the absolute loudest peaks found
        int offset = static_cast<int>(i2s1_loudest_index - i2s0_loudest_index);
        peak_offsets[clap_count] = offset;
        
        Serial.printf("*** CLAP %d COMPLETE ***\n", clap_count + 1);
        Serial.printf("I2S0 loudest: %d @sample %ld\n", i2s0_loudest_val, i2s0_loudest_index);
        Serial.printf("I2S1 loudest: %d @sample %ld\n", i2s1_loudest_val, i2s1_loudest_index);
        Serial.printf("Offset: %d samples (%.2f ms)\n\n", offset, (float)offset * 1000.0f / SAMPLE_RATE_HZ);
        
        if (abs((float)offset * 1000.0f / SAMPLE_RATE_HZ) > 10.0f) {
          clap_count++;
        } else {
          Serial.println("  Offset not within expectation. Ignoring clap. ");
        }
        last_peak_time = millis();
        tracking_clap = false;
      }
    } else if (peak0_val > CLAP_THRESHOLD || peak1_val > CLAP_THRESHOLD) {
      // Peak detected but too soon after last one - show info
      float time_since_last = (float)(millis() - last_peak_time) / 1000.0f;
      Serial.printf("[Volume: %d] Potential clap detected but spacing too short (%.2fs since last, need %.2fs)\n", 
                    peak0_val > peak1_val ? peak0_val : peak1_val, 
                    time_since_last, (float)MIN_CLAP_SPACING / SAMPLE_RATE_HZ);
    } else {
      // Live volume readout
      int max_peak = peak0_val > peak1_val ? peak0_val : peak1_val;
      if (max_peak > baseline_amplitude) {
        Serial.printf("[Volume: %d] (Threshold: %d, Above baseline: %.0f%%)\n", 
                      max_peak, CLAP_THRESHOLD, 
                      (float)(max_peak - baseline_amplitude) / (float)baseline_amplitude * 100.0f);
      }
    }

    delay(10);
  }

  // Average the offsets
  long sum_offset = 0;
  for (int i = 0; i < NUM_CLAPS_FOR_CALIBRATION; ++i) {
    sum_offset += peak_offsets[i];
  }
  i2s1_sample_offset = sum_offset / NUM_CLAPS_FOR_CALIBRATION;

  // Clamp to valid range
  if (i2s1_sample_offset > MAX_OFFSET_SAMPLES) {
    i2s1_sample_offset = MAX_OFFSET_SAMPLES;
  } else if (i2s1_sample_offset < -MAX_OFFSET_SAMPLES) {
    i2s1_sample_offset = -MAX_OFFSET_SAMPLES;
  }

  Serial.printf("\n=== CALIBRATION COMPLETE ===\n");
  Serial.printf("Baseline volume: %d\n", baseline_amplitude);
  Serial.printf("Clap threshold: %d (%.2f dB above baseline)\n", CLAP_THRESHOLD, 20.0f * log10f(CLAP_THRESHOLD_MULTIPLIER));
  Serial.printf("Measured I2S1 offset: %d samples (%.2f ms)\n", 
                i2s1_sample_offset, (float)i2s1_sample_offset * 1000.0f / SAMPLE_RATE_HZ);

  // Align channels by discarding samples from the leading channel
  if (i2s1_sample_offset > 0) {
    // I2S1 lags I2S0, so discard samples from I2S1 to catch up
    Serial.printf("Aligning channels: discarding %d samples from I2S1...\n", i2s1_sample_offset);
    
    int samples_to_discard = i2s1_sample_offset;
    
    while (samples_to_discard > 0) {
      size_t bytes_to_read = (samples_to_discard > FRAMES_PER_CHUNK) ? 
                              (FRAMES_PER_CHUNK * sizeof(int32_t)) : 
                              (samples_to_discard * sizeof(int32_t));
      size_t bytes_read = 0;
      i2s_read(I2S_NUM_1, discard_buffer, bytes_to_read, &bytes_read, 1000);
      int samples_discarded = bytes_read / sizeof(int32_t);
      samples_to_discard -= samples_discarded;
      Serial.printf("  Discarded %d samples, %d remaining...\n", samples_discarded, samples_to_discard);
      if (bytes_read == 0) {
        Serial.println("  Warning: No data read, aborting discard.");
        break;
      }
    }
    Serial.println("Alignment complete.");
  } else if (i2s1_sample_offset < 0) {
    // I2S0 lags I2S1, so discard samples from I2S0 to catch up
    int abs_offset = -i2s1_sample_offset;
    Serial.printf("Aligning channels: discarding %d samples from I2S0...\n", abs_offset);
    
    int samples_to_discard = abs_offset;
    int32_t discard_buffer[FRAMES_PER_CHUNK * 2];  // I2S0 is stereo
    
    while (samples_to_discard > 0) {
      size_t bytes_to_read = (samples_to_discard > FRAMES_PER_CHUNK) ? 
                              (FRAMES_PER_CHUNK * 2 * sizeof(int32_t)) : 
                              (samples_to_discard * 2 * sizeof(int32_t));
      size_t bytes_read = 0;
      i2s_read(I2S_NUM_0, discard_buffer, bytes_to_read, &bytes_read, 1000);
      int samples_discarded = bytes_read / (sizeof(int32_t) * 2);  // Stereo frames
      samples_to_discard -= samples_discarded;
      Serial.printf("  Discarded %d samples, %d remaining...\n", samples_discarded, samples_to_discard);
      if (bytes_read == 0) {
        Serial.println("  Warning: No data read, aborting discard.");
        break;
      }
    }
    Serial.println("Alignment complete.");
  } else {
    Serial.println("Channels already aligned (offset = 0).");
  }

  calibration_complete = true;
  Serial.println("Ready to stream audio...\n");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  setup_wifi();
  setup_i2s();
  
  // Initialize single UDP socket for combined 3-channel transmission
  if (!udp_combined.begin(0)) {
    Serial.println("Failed to initialize UDP socket for combined 3-channel stream");
  } else {
    Serial.println("UDP combined stream initialized on port 30001");
  }
  
  Serial.println("Setup complete, starting calibration...");
  
  // Run calibration sequence
  run_calibration();
}

void loop() {
  // Handle serial commands (including manual calibration mode entry)
  handle_serial_command();
  
  // Wait for calibration to complete
  if (!calibration_complete) {
    delay(100);
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    setup_wifi();
  }

  unsigned long loop_start = 0;
  if (ENABLE_TIMING_DIAGNOSTICS) {
    loop_start = millis();
  }

  size_t bytes_read0 = 0;
  size_t bytes_read1 = 0;
  size_t frames = 0;

  unsigned long i2s_start = 0;
  unsigned long i2s0_read_time_us = 0;
  if (ENABLE_TIMING_DIAGNOSTICS) {
    i2s_start = millis();
  }
  if (ENABLE_TEST_MODE) {
    // Test mode: generate synthetic waveforms instead of reading I2S
    // All three channels use aligned frequencies for synchronized signal testing
    frames = FRAMES_PER_CHUNK;
    
    // Generate test signals: sine (left), square (right), triangle (center)
    for (size_t i = 0; i < frames; ++i) {
      int32_t sine_sample = generate_sine_sample(test_phase_index + i);
      int32_t square_sample = generate_triangle_sample(test_phase_index + i);
      int32_t triangle_sample = generate_triangle_sample(test_phase_index + i);
      
      i2s0_raw[i * 2] = sine_sample;      // MIC0 Left: Sine
      i2s0_raw[i * 2 + 1] = square_sample; // MIC0 Right: Square
      i2s1_raw[i] = triangle_sample;       // MIC1 Center: Triangle
    }
    test_phase_index += frames;  // Advance global phase for next chunk
  } else {
    // Read both I2S ports (hardware-synchronized via shared WS/BCLK)
    // Serial.println("Attempting I2s Read. ");
    int portTICKDELAY = 1000; // 1000 ticks
    
    i2s_read(I2S_NUM_0, i2s0_raw, sizeof(i2s0_raw), &bytes_read0, portTICKDELAY);
    // Serial.println("I2S0: Bytes read - " + String(bytes_read0));
    i2s_read(I2S_NUM_1, i2s1_raw, sizeof(i2s1_raw), &bytes_read1, portTICKDELAY);
    // Serial.println("I2S1: Bytes read - " + String(bytes_read1));

    size_t frames0 = bytes_read0 / (sizeof(int32_t) * 2);
    size_t frames1 = bytes_read1 / (sizeof(int32_t) * 2);  // I2S1 reads stereo frames even in mono mode
    frames = FRAMES_PER_CHUNK;
    
    // Track time between successful I2S0 reads
    if (bytes_read0 > 0) {
      unsigned long now = millis();
      if (last_i2s0_read_time > 0) {
        i2s0_read_interval_ms = now - last_i2s0_read_time;
      }
      last_i2s0_read_time = now;
    }
  }

  if (ENABLE_TIMING_DIAGNOSTICS && !ENABLE_TEST_MODE) {
    unsigned long i2s_time = millis() - i2s_start;
    total_i2s_read_time += i2s_time;
  }

  if (frames == 0) {
    delay(5);
    return;
  }

  // Convert/pack phase
  // Channels are now aligned from calibration - no runtime offset needed
  unsigned long convert_start = 0;
  if (ENABLE_TIMING_DIAGNOSTICS) {
    convert_start = millis();
  }

  for (size_t i = 0; i < frames; ++i) {
    // Apply manual calibration offsets: skip leading samples from specified buffers
    size_t i2s0_idx = i + manual_discard_i2s0_samples;
    size_t i2s1_idx = i + manual_discard_i2s1_samples;
    
    // Bounds check to avoid reading beyond buffer
    if (i2s0_idx >= frames || i2s1_idx >= frames) {
      // If we've run out of valid samples due to discarding, pad with zeros
      pcm_out[i * 3] = 0;
      pcm_out[i * 3 + 1] = 0;
      pcm_out[i * 3 + 2] = 0;
      continue;
    }
    
    int32_t left0 = convert_sample(i2s0_raw[i2s0_idx * 2]);
    int32_t right0 = convert_sample(i2s0_raw[i2s0_idx * 2 + 1]);
    int32_t center = convert_sample(i2s1_raw[i2s1_idx]);  

    // Tag each sample with channel ID and timing index
    // Channel IDs: 0=MIC0Left, 1=MIC0Right, 2=MIC1Center
    // Timing index (bits 3-5): increments with each frame
    int32_t left0_tagged = tag_sample_with_timing_and_channel(left0, 0, timing_index);
    int32_t right0_tagged = tag_sample_with_timing_and_channel(right0, 1, timing_index);
    int32_t center_tagged = tag_sample_with_timing_and_channel(center, 2, timing_index);

    // Output: 3-channel interleaved [L0, R0, Center, L0, R0, Center, ...]
    pcm_out[i * 3] = left0_tagged;
    pcm_out[i * 3 + 1] = right0_tagged;
    pcm_out[i * 3 + 2] = center_tagged;
    
    // Increment timing index for next frame
    timing_index = (timing_index + 1) & 0x7;
    // printf("%lu frame: L=0x%08X, R=0x%08X, C=0x%08X\n", i, left0_tagged, right0_tagged, center_tagged);
  }

  if (ENABLE_TIMING_DIAGNOSTICS) {
    unsigned long convert_time = millis() - convert_start;
    total_convert_time += convert_time;
    // printf("SIZE OF pcm_out: %lu bytes\n", sizeof(pcm_out)); // 512 frames
    // printf("Frame count: %lu\n", frames);
  }

  total_frames_captured += frames;
  chunk_count++;
  
  // CRITICAL: Only send if NOT in manual calibration mode
  if (!manual_calibration_mode) {
    // CRITICAL: Send all 3 channels in a SINGLE atomic UDP packet
    // Packet format (interleaved): [L, R, C, L, R, C, ...]
    // This ensures all 3 channels arrive together on the server, eliminating timing skew
    bool ok = send_udp_combined(udp_combined, UDP_PORT_COMBINED, pcm_out, frames);
  } else {
    // In manual calibration mode, skip transmission
  }

  // send_audio_to_serial();
  
  if (ENABLE_TIMING_DIAGNOSTICS) {
    unsigned long loop_time = millis() - loop_start;
    total_loop_time += loop_time;
  }
  
  // Print detailed timing every 10 seconds
  unsigned long now = millis();
  if (ENABLE_TIMING_DIAGNOSTICS && (now - last_diagnostic_time >= 10000)) {
    last_diagnostic_time = now;
    
    float elapsed_sec = 10.0;
    float avg_bitrate_kbps = (total_bytes_uploaded * 8) / 1000.0 / elapsed_sec;
    float audio_duration_sec = (float)total_frames_captured / SAMPLE_RATE_HZ;
    float avg_chunk_bytes = chunk_count > 0 ? (float)total_bytes_uploaded / chunk_count : 0;
    float avg_i2s_time = chunk_count > 0 ? (float)total_i2s_read_time / chunk_count : 0;
    float avg_convert_time = chunk_count > 0 ? (float)total_convert_time / chunk_count : 0;
    float avg_loop_time = chunk_count > 0 ? (float)total_loop_time / chunk_count : 0;
    float avg_upload_time = chunk_count > 0 ? (float)(max_upload_time_ms + min_upload_time_ms) / 2.0 : 0;
    
    Serial.println("\n=== AUDIO DIAGNOSTICS (10 sec interval) ===");
    if (ENABLE_TEST_MODE) {
      Serial.printf("*** TEST MODE ACTIVE ***\n");
      Serial.printf("Test frequency: %.1f Hz (Sine on Left, Square on Right, Triangle on Center)\n", TEST_FREQUENCY_HZ);
      Serial.printf("Global phase index: %zu samples\n", test_phase_index);
    }
    if (manual_calibration_mode) {
      Serial.printf("*** MANUAL CALIBRATION MODE ACTIVE ***\n");
      Serial.printf("I2S0 discard: %d samples, I2S1 discard: %d samples\n", 
                    manual_discard_i2s0_samples, manual_discard_i2s1_samples);
    }
    Serial.printf("Chunks captured: %d\n", chunk_count);
    Serial.printf("Total frames: %lu (%.2f sec of audio)\n", total_frames_captured, audio_duration_sec);
    Serial.printf("Total bytes uploaded: %lu\n", total_bytes_uploaded);
    Serial.printf("Avg chunk size: %.0f bytes\n", avg_chunk_bytes);
    Serial.printf("Bitrate: %.1f kbps\n", avg_bitrate_kbps);
    Serial.printf("Sample rate: %d Hz (%.2f ms per chunk)\n", SAMPLE_RATE_HZ, (float)FRAMES_PER_CHUNK * 1000.0 / SAMPLE_RATE_HZ);
    Serial.printf("WiFi RSSI: %d dBm\n", WiFi.RSSI());
    
    Serial.println("\n--- PACKET INTEGRITY & ALIGNMENT ---");
    unsigned int total_packets = packets_sent_combined + packets_failed_combined;
    float success_rate = total_packets > 0 ? (float)packets_sent_combined * 100.0 / total_packets : 0.0;
    Serial.printf("Combined 3-channel packets: %u sent, %u failed\n", packets_sent_combined, packets_failed_combined);
    Serial.printf("Success rate: %.2f%%\n", success_rate);
    Serial.printf("Sample-perfect aligned packets: %u (all 3 channels in single UDP packet)\n", sync_perfect);
    
    Serial.println("\n--- TRANSMISSION OPTIMIZATION ---");
    Serial.printf("Transmission method: Single atomic UDP packet (port 30001)\n");
    Serial.printf("Packet format: 3-channel interleaved [L,R,C,L,R,C,...]\n");
    
    Serial.println("\n--- TIMING BREAKDOWN (ms per chunk) ---");
    Serial.printf("I2S read: %.2f ms (avg)\n", avg_i2s_time);
    Serial.printf("Convert/pack: %.2f ms (avg)\n", avg_convert_time);
    Serial.printf("Upload: %.2f ms (avg), min: %lu ms, max: %lu ms\n", avg_upload_time, min_upload_time_ms, max_upload_time_ms);
    Serial.printf("Total loop: %.2f ms (avg)\n", avg_loop_time);
    Serial.printf("I2S0 read interval: %lu ms (time between successful reads)\n", i2s0_read_interval_ms);
    Serial.println("=========================================\n");
    
    // Reset for next interval
    total_frames_captured = 0;
    total_bytes_uploaded = 0;
    chunk_count = 0;
    max_upload_time_ms = 0;
    min_upload_time_ms = ULONG_MAX;
    total_i2s_read_time = 0;
    total_convert_time = 0;
    total_loop_time = 0;
    packets_sent_combined = 0;
    packets_failed_combined = 0;
    sync_perfect = 0;
    // NOTE: Timing offset variables no longer used (hardware synchronization handles this)
  }
}