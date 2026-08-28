/*
 * Persistent stdin/stdout Steam Audio PCM bridge for PlanetXRobot.
 * Input and output are interleaved stereo signed 16-bit PCM at 48 kHz.
 */
#include <phonon.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define SAMPLE_RATE 48000
#define BLOCK_SIZE 1024
#define ORBIT_SECONDS 8.0
#define PI 3.14159265358979323846
#define SIDE_PHASE(value) atan2(2.4 * sin(value), cos(value))
#define LATERAL_EMPHASIS_DB 3.0f
#define EMPHASIS_HIGHPASS_ALPHA 0.884f

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
    IPLContextSettings context_settings = {0};
    context_settings.version = STEAMAUDIO_VERSION;
    IPLContext context = NULL;
    if (iplContextCreate(&context_settings, &context)) return 2;

    IPLAudioSettings audio_settings = {SAMPLE_RATE, BLOCK_SIZE};
    IPLHRTFSettings hrtf_settings = {0};
    hrtf_settings.type = IPL_HRTFTYPE_DEFAULT;
    hrtf_settings.volume = 1.0f;
    hrtf_settings.normType = IPL_HRTFNORMTYPE_RMS;
    IPLHRTF hrtf = NULL;
    if (iplHRTFCreate(context, &audio_settings, &hrtf_settings, &hrtf)) return 3;

    IPLBinauralEffectSettings effect_settings = {0};
    effect_settings.hrtf = hrtf;
    IPLBinauralEffect effect = NULL;
    if (iplBinauralEffectCreate(context, &audio_settings, &effect_settings, &effect)) return 4;

    int16_t input_pcm[BLOCK_SIZE * 2], output_pcm[BLOCK_SIZE * 2];
    float program[BLOCK_SIZE], left[BLOCK_SIZE], right[BLOCK_SIZE];
    float previous_input[2] = {0.0f, 0.0f};
    float previous_high[2] = {0.0f, 0.0f};
    float *input_channels[] = {program};
    float *output_channels[] = {left, right};
    IPLAudioBuffer input = {1, BLOCK_SIZE, input_channels};
    IPLAudioBuffer output = {2, BLOCK_SIZE, output_channels};
    uint64_t frames = 0;

    while (fread(input_pcm, sizeof(int16_t) * 2, BLOCK_SIZE, input_stream) == BLOCK_SIZE) {
        for (int i = 0; i < BLOCK_SIZE; ++i)
            program[i] = ((float) input_pcm[2 * i] + (float) input_pcm[2 * i + 1]) / 65536.0f;

        const double phase = SIDE_PHASE(
            2.0 * PI * (start_position + (double) frames / SAMPLE_RATE) / ORBIT_SECONDS
        );
        IPLBinauralEffectParams params = {0};
        params.direction = (IPLVector3){(IPLfloat32) sin(phase), 0, (IPLfloat32) cos(phase)};
        params.interpolation = IPL_HRTFINTERPOLATION_BILINEAR;
        params.spatialBlend = 1.0f;
        params.hrtf = hrtf;
        iplBinauralEffectApply(effect, &params, &input, &output);

        // Preserve the HRTF-rendered full-band program while subtly reinforcing
        // its localization-critical band. The equal-power gains are direction
        // linked, and the side-only compensation prevents a loudness rise.
        const float emphasis = LATERAL_EMPHASIS_DB * (float) sin(phase);
        const float left_gain = powf(10.0f, -emphasis / 20.0f);
        const float right_gain = powf(10.0f, emphasis / 20.0f);
        const float gain_normalizer = sqrtf(
            2.0f / (left_gain * left_gain + right_gain * right_gain)
        );
        const float loudness_compensation =
            1.0f - 0.08f * (float) sin(phase) * (float) sin(phase);
        for (int i = 0; i < BLOCK_SIZE; ++i) {
            float *ears[] = {&left[i], &right[i]};
            const float gains[] = {
                left_gain * gain_normalizer,
                right_gain * gain_normalizer,
            };
            for (int ear = 0; ear < 2; ++ear) {
                const float original = *ears[ear];
                const float high = EMPHASIS_HIGHPASS_ALPHA * (
                    previous_high[ear] + original - previous_input[ear]
                );
                previous_input[ear] = original;
                previous_high[ear] = high;
                *ears[ear] = (
                    original + high * (gains[ear] - 1.0f)
                ) * loudness_compensation;
            }
            output_pcm[2 * i] = (int16_t) lrintf(clamp_output(left[i]) * 32767.0f);
            output_pcm[2 * i + 1] = (int16_t) lrintf(clamp_output(right[i]) * 32767.0f);
        }
        if (fwrite(output_pcm, sizeof(int16_t) * 2, BLOCK_SIZE, stdout) != BLOCK_SIZE) break;
        frames += BLOCK_SIZE;
    }

    fflush(stdout);
    const int input_error = ferror(input_stream);
    if (input_stream != stdin) fclose(input_stream);
    iplBinauralEffectRelease(&effect);
    iplHRTFRelease(&hrtf);
    iplContextRelease(&context);
    if (decoder_pid > 0) {
        int decoder_status;
        waitpid(decoder_pid, &decoder_status, 0);
        if (!WIFEXITED(decoder_status) || WEXITSTATUS(decoder_status) != 0) return 8;
    }
    return input_error || ferror(stdout);
}
