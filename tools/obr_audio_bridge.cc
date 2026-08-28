/*
 * Persistent stdin/stdout Open Binaural Renderer PCM bridge for PlanetXRobot.
 * Input and output are interleaved stereo signed 16-bit PCM at 48 kHz.
 *
 * Build this against Google's OBR source tree (https://github.com/google/obr)
 * as an executable linked to its `obr` target. The deployed binary must be
 * installed as /usr/local/bin/planetx-obr-audio-bridge.
 */
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

#include "obr/audio_buffer/audio_buffer.h"
#include "obr/renderer/audio_element_config.h"
#include "obr/renderer/audio_element_type.h"
#include "obr/renderer/obr_impl.h"

#define SAMPLE_RATE 48000
#define BLOCK_SIZE 1024
#define ORBIT_SECONDS 8.0
#define PI 3.14159265358979323846
#define SIDE_PHASE(value) atan2(2.4 * sin(value), cos(value))

static float clamp_output(float value) {
    return value > 0.98f ? 0.98f : (value < -0.98f ? -0.98f : value);
}

int main(int argc, char **argv) {
    FILE *input_stream = stdin;
    pid_t decoder_pid = -1;
    double start_position = argc > 1 ? strtod(argv[1], NULL) : 0.0;
    if (argc > 4 && strcmp(argv[1], "--ffmpeg") == 0 && strcmp(argv[3], "--") == 0) {
        int pipefd[2];
        start_position = strtod(argv[2], NULL);
        if (pipe(pipefd) != 0) return 5;
        decoder_pid = fork();
        if (decoder_pid < 0) return 6;
        if (decoder_pid == 0) {
            close(pipefd[0]);
            if (dup2(pipefd[1], STDOUT_FILENO) < 0) _exit(127);
            close(pipefd[1]);
            execvp(argv[4], &argv[4]);
            _exit(127);
        }
        close(pipefd[1]);
        input_stream = fdopen(pipefd[0], "rb");
        if (input_stream == NULL) return 7;
    }

    obr::ObrImpl renderer(BLOCK_SIZE, SAMPLE_RATE);
    if (!renderer.AddAudioElement(
            obr::AudioElementType::kObjectMono,
            obr::BinauralFilterProfile::kDirect
        ).ok()) {
        return 2;
    }

    int16_t input_pcm[BLOCK_SIZE * 2], output_pcm[BLOCK_SIZE * 2];
    uint64_t frames = 0;
    while (fread(input_pcm, sizeof(int16_t) * 2, BLOCK_SIZE, input_stream) == BLOCK_SIZE) {
        const double phase = SIDE_PHASE(
            2.0 * PI * (start_position + (double) frames / SAMPLE_RATE) / ORBIT_SECONDS
        );
        // OBR's object azimuth convention is opposite the existing Steam path.
        if (!renderer.UpdateObjectPosition(
                0, (float) (-phase * 180.0 / PI), 0.0f, 1.0f
            ).ok()) {
            return 3;
        }

        obr::AudioBuffer input(1, BLOCK_SIZE);
        obr::AudioBuffer output(2, BLOCK_SIZE);
        for (int i = 0; i < BLOCK_SIZE; ++i) {
            input[0][i] = (
                (float) input_pcm[2 * i] + (float) input_pcm[2 * i + 1]
            ) / 65536.0f;
        }
        renderer.Process(input, &output);
        for (int i = 0; i < BLOCK_SIZE; ++i) {
            output_pcm[2 * i] = (int16_t) lrintf(
                clamp_output(output[0][i]) * 32767.0f
            );
            output_pcm[2 * i + 1] = (int16_t) lrintf(
                clamp_output(output[1][i]) * 32767.0f
            );
        }
        if (fwrite(output_pcm, sizeof(int16_t) * 2, BLOCK_SIZE, stdout) != BLOCK_SIZE) break;
        frames += BLOCK_SIZE;
    }

    fflush(stdout);
    const int input_error = ferror(input_stream);
    if (input_stream != stdin) fclose(input_stream);
    if (decoder_pid > 0) {
        int decoder_status;
        waitpid(decoder_pid, &decoder_status, 0);
        if (!WIFEXITED(decoder_status) || WEXITSTATUS(decoder_status) != 0) return 8;
    }
    return input_error || ferror(stdout);
}
