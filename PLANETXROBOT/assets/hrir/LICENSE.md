# KEMAR HRIR subset

This directory contains a compact, 48 kHz WAV subset derived from the
measured KEMAR HRIR collection by Bill Gardner and Keith Martin, MIT Media
Laboratory (1994).

Source: https://sound.media.mit.edu/resources/KEMAR.html

The source page states that the data is Copyright 1994 by the MIT Media
Laboratory and is provided free with no restrictions on research or commercial
use, provided the authors are cited. Retain this notice and cite the authors in
any redistribution or product documentation.

Source data: compact KEMAR HRIR package, horizontal plane (elevation 0°),
44.1 kHz, stereo signed-16-bit WAV, 128-sample impulse responses.

Preprocessing: FFmpeg 8.0.1 resampled the selected measured responses to
48 kHz stereo PCM WAV. Positions 225°, 270°, and 315° are channel-swapped
mirrors of the corresponding 135°, 90°, and 45° measured KEMAR responses.
No synthetic impulse response was generated.
