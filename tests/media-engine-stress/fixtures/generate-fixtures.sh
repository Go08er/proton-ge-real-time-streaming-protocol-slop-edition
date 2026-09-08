#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -euo pipefail

die() {
    printf 'fixture generation: %s\n' "$*" >&2
    exit 1
}

require_env() {
    local name=$1
    [[ -n "${!name:-}" ]] || die "required environment variable is empty: $name"
}

for name in \
    FIXTURE_ROOT FFMPEG_BIN PYTHON_BIN FONT_FILE PROFILE_NAME \
    PRIMARY_DURATION TOPOLOGY_DURATION HLS_DURATION MATRIX_DURATION WIDTH HEIGHT FPS \
    AUDIO_RATE AUDIO_CHANNELS GOP_SECONDS DELAY_SECONDS HLS_SEGMENT_SECONDS \
    HLS_LIVE_WINDOW LIVE_SCHEDULE_TOOL PAIRED_LIVE_SCHEDULE_TOOL \
    ALIGN_LIVE_RENDITIONS_TOOL OBSERVED_RTSP_FIXTURE_SHA256; do
    require_env "$name"
done

[[ "$FIXTURE_ROOT" != / ]] || die 'refusing to use / as a fixture root'
[[ ! -e "$FIXTURE_ROOT" ]] || die "fixture root already exists: $FIXTURE_ROOT"
[[ -x "$FFMPEG_BIN" ]] || die "FFmpeg is not executable: $FFMPEG_BIN"
[[ -x "$PYTHON_BIN" ]] || die "Python is not executable: $PYTHON_BIN"
[[ -r "$FONT_FILE" ]] || die "font is not readable: $FONT_FILE"
[[ -r "$LIVE_SCHEDULE_TOOL" ]] || die "live schedule tool is not readable: $LIVE_SCHEDULE_TOOL"
[[ -r "$PAIRED_LIVE_SCHEDULE_TOOL" ]] || die "paired live schedule tool is not readable: $PAIRED_LIVE_SCHEDULE_TOOL"
[[ -r "$ALIGN_LIVE_RENDITIONS_TOOL" ]] || die "live alignment tool is not readable: $ALIGN_LIVE_RENDITIONS_TOOL"

mkdir -p \
    "$FIXTURE_ROOT/av" \
    "$FIXTURE_ROOT/topology" \
    "$FIXTURE_ROOT/hls/vod/segments" \
    "$FIXTURE_ROOT/hls/live/segments" \
    "$FIXTURE_ROOT/hls/vod-separate/video/segments" \
    "$FIXTURE_ROOT/hls/vod-separate/audio/segments" \
    "$FIXTURE_ROOT/hls/live-separate/video/segments" \
    "$FIXTURE_ROOT/hls/live-separate/audio/segments" \
    "$FIXTURE_ROOT/hls/cmaf/segments" \
    "$FIXTURE_ROOT/rtsp" \
    "$FIXTURE_ROOT/discovery/containers" \
    "$FIXTURE_ROOT/discovery/codecs" \
    "$FIXTURE_ROOT/discovery/audio" \
    "$FIXTURE_ROOT/invalid" \
    "$FIXTURE_ROOT/provenance"

command_log="$FIXTURE_ROOT/provenance/ffmpeg-commands.sh"
printf '# Reproducible FFmpeg command transcript (paths are relative to the fixture root).\n' \
    >"$command_log"
printf 'set -euo pipefail\n\n' >>"$command_log"

run_ffmpeg() {
    local argument output_index output_path
    local -a ffmpeg_args=("$@")
    ((${#ffmpeg_args[@]} >= 1)) || die 'FFmpeg command has no output path'
    output_index=$((${#ffmpeg_args[@]} - 1))
    output_path=${ffmpeg_args[$output_index]}
    unset "ffmpeg_args[$output_index]"
    # `-fflags` is an input/output option. Placing it only in `common` before
    # the inputs does not make Matroska/WebM/Ogg muxer UIDs and serials
    # deterministic. Inject it immediately before every output path.
    ffmpeg_args+=(-fflags +bitexact "$output_path")
    printf 'ffmpeg' >>"$command_log"
    for argument in "${ffmpeg_args[@]}"; do
        printf ' %q' "$argument" >>"$command_log"
    done
    printf '\n' >>"$command_log"
    "$FFMPEG_BIN" "${ffmpeg_args[@]}"
}

cd "$FIXTURE_ROOT"

gop_frames=$((FPS * GOP_SECONDS))
fragment_microseconds=$((HLS_SEGMENT_SECONDS * 1000000))

# The frame number and media PTS are visible. Both audio channels carry a
# continuous low-level carrier plus a 100 ms marker whose frequency is unique
# for every second of the full qualification fixture. A stale sample from ten
# seconds earlier therefore cannot satisfy the marker oracle.
video_source="testsrc2=size=${WIDTH}x${HEIGHT}:rate=${FPS}:duration=${PRIMARY_DURATION},format=yuv420p,drawtext=fontfile=${FONT_FILE}:text='frame=%{n} time=%{pts\\:hms}':x=8:y=8:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.70"
audio_source="aevalsrc=exprs='0.02*sin(2*PI*173*t)+0.12*sin(2*PI*(400+20*floor(t))*t)*lt(mod(t\\,1)\\,0.10)|0.02*sin(2*PI*257*t)+0.12*sin(2*PI*(3400+20*floor(t))*t)*lt(mod(t\\,1)\\,0.10)':s=${AUDIO_RATE}:d=${PRIMARY_DURATION}:c=stereo"

common=(
    -hide_banner -nostdin -loglevel error -y
    -fflags +bitexact
)

run_ffmpeg "${common[@]}" \
    -f lavfi -i "$video_source" \
    -f lavfi -i "$audio_source" \
    -map 0:v:0 -map 1:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v libx264 -preset medium -profile:v main -pix_fmt yuv420p \
    -g "$gop_frames" -keyint_min "$gop_frames" -sc_threshold 0 \
    -threads:v 1 -x264-params 'threads=1:lookahead_threads=1:sliced_threads=0' \
    -flags:v +bitexact \
    -c:a aac -b:a 128k -ar "$AUDIO_RATE" -ac "$AUDIO_CHANNELS" \
    -flags:a +bitexact \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -shortest -f matroska provenance/base.mkv

run_ffmpeg "${common[@]}" -i provenance/base.mkv \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +faststart av/faststart.mp4

run_ffmpeg "${common[@]}" -i provenance/base.mkv \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags -faststart av/tail-moov.mp4

run_ffmpeg "${common[@]}" -i provenance/base.mkv \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +frag_keyframe+empty_moov+default_base_moof \
    -frag_duration "$fragment_microseconds" av/fragmented.mp4

if [[ "$PROFILE_NAME" == full ]]; then
    ladder_duration=45
    ladder_audio_source="aevalsrc=exprs='0.02*sin(2*PI*173*t)+0.12*sin(2*PI*(400+20*floor(t))*t)*lt(mod(t\\,1)\\,0.10)|0.02*sin(2*PI*257*t)+0.12*sin(2*PI*(3400+20*floor(t))*t)*lt(mod(t\\,1)\\,0.10)':s=48000:d=${ladder_duration}:c=stereo"
    mkdir -p provenance/rtsp-ladder
    run_ffmpeg "${common[@]}" \
        -f lavfi -i "$ladder_audio_source" \
        -map 0:a:0 -map_metadata -1 -map_chapters -1 \
        -c:a aac -b:a 96000 -ar 48000 -ac 2 -flags:a +bitexact \
        -metadata creation_time=1970-01-01T00:00:00Z \
        provenance/rtsp-ladder/audio.m4a

    generate_rtsp_ladder_rung() {
        local name=$1 width=$2 height=$3 profile=$4 bitrate=$5
        local buffer=$((bitrate * 2))
        local ladder_video_source
        ladder_video_source="testsrc2=size=${width}x${height}:rate=30:duration=${ladder_duration},format=yuv420p,drawtext=fontfile=${FONT_FILE}:text='frame=%{n} time=%{pts\\:hms}':x=8:y=8:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.70"
        run_ffmpeg "${common[@]}" \
            -f lavfi -i "$ladder_video_source" \
            -map 0:v:0 -map_metadata -1 -map_chapters -1 -an \
            -c:v libx264 -preset medium -profile:v "$profile" -pix_fmt yuv420p \
            -b:v "$bitrate" -minrate "$bitrate" -maxrate "$bitrate" -bufsize "$buffer" \
            -g 30 -keyint_min 30 -sc_threshold 0 -refs 3 -bf 2 \
            -threads:v 1 \
            -x264-params 'threads=1:lookahead_threads=1:sliced_threads=0:nal-hrd=cbr:force-cfr=1' \
            -flags:v +bitexact -metadata creation_time=1970-01-01T00:00:00Z \
            "provenance/rtsp-ladder/${name}-video.mp4"
        run_ffmpeg "${common[@]}" \
            -i "provenance/rtsp-ladder/${name}-video.mp4" \
            -i provenance/rtsp-ladder/audio.m4a \
            -map 0:v:0 -map 1:a:0 -map_metadata -1 -map_chapters -1 -c copy \
            -metadata creation_time=1970-01-01T00:00:00Z \
            -shortest -movflags +faststart "rtsp/${name}.mp4"
    }

    generate_rtsp_ladder_rung payload-a-320x180-main-270k 320 180 main 270000
    generate_rtsp_ladder_rung payload-b-1280x720-main-270k 1280 720 main 270000
    generate_rtsp_ladder_rung payload-c-1280x720-high-270k 1280 720 high 270000
    generate_rtsp_ladder_rung payload-d-1280x720-high-8200k 1280 720 high 8200000
    rm -f provenance/rtsp-ladder/audio.m4a provenance/rtsp-ladder/*-video.mp4
    rmdir provenance/rtsp-ladder

    observed_path=rtsp/repro-observed-heavy-v1.mp4
    observed_record=provenance/rtsp-observed-heavy-source.txt
    if [[ -n "${OBSERVED_RTSP_FIXTURE:-}" ]]; then
        [[ -f "$OBSERVED_RTSP_FIXTURE" && ! -L "$OBSERVED_RTSP_FIXTURE" ]] ||
            die 'observed RTSP fixture is not a regular file'
        observed_sha256=$(sha256sum "$OBSERVED_RTSP_FIXTURE" | cut -d ' ' -f 1)
        [[ "$observed_sha256" == "$OBSERVED_RTSP_FIXTURE_SHA256" ]] ||
            die 'observed RTSP fixture hash differs from its reviewed capture'
        install -m 0444 "$OBSERVED_RTSP_FIXTURE" "$observed_path"
        printf 'mode=captured-reference\nsha256=%s\n' \
            "$observed_sha256" >"$observed_record"
    else
        run_ffmpeg "${common[@]}" \
            -f lavfi -i 'testsrc2=size=1280x720:rate=30:duration=45' \
            -f lavfi -i 'sine=frequency=1000:sample_rate=48000:duration=45' \
            -map 0:v:0 -map 1:a:0 -map_metadata -1 -map_chapters -1 \
            -c:v libx264 -preset veryfast -profile:v high -pix_fmt yuv420p \
            -b:v 8000000 -minrate 8000000 -maxrate 8000000 -bufsize 16000000 \
            -g 60 -keyint_min 60 -sc_threshold 0 \
            -threads:v 6 \
            -x264-params 'threads=6:lookahead_threads=2:sliced_threads=0:nal-hrd=cbr:force-cfr=1' \
            -flags:v +bitexact \
            -c:a aac -b:a 128000 -ar 48000 -ac 2 -flags:a +bitexact \
            -metadata creation_time=1970-01-01T00:00:00Z \
            -shortest -movflags +faststart "$observed_path"
        printf 'mode=deterministic-reconstruction\nreference_sha256=%s\n' \
            "$OBSERVED_RTSP_FIXTURE_SHA256" >"$observed_record"
    fi
    run_ffmpeg "${common[@]}" \
        -i "$observed_path" -ss 0.500 -t 44.000 \
        -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
        -c copy -copyinkf -avoid_negative_ts make_zero \
        -metadata creation_time=1970-01-01T00:00:00Z \
        -movflags +faststart rtsp/repro-observed-midgop-v1.mp4
fi

run_ffmpeg "${common[@]}" -i provenance/base.mkv -t "$TOPOLOGY_DURATION" \
    -map 0:a:0 -map_metadata -1 -map_chapters -1 -c:a copy \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +faststart topology/audio-only.m4a

run_ffmpeg "${common[@]}" -i provenance/base.mkv -t "$TOPOLOGY_DURATION" \
    -map 0:v:0 -map_metadata -1 -map_chapters -1 -c:v copy \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +faststart topology/video-only.mp4

run_ffmpeg "${common[@]}" -i topology/video-only.mp4 \
    -itsoffset "$DELAY_SECONDS" -i topology/audio-only.m4a \
    -map 0:v:0 -map 1:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -copyts -t "$TOPOLOGY_DURATION" -avoid_negative_ts disabled \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +faststart topology/delayed-audio.mp4

run_ffmpeg "${common[@]}" -i topology/audio-only.m4a \
    -itsoffset "$DELAY_SECONDS" -i topology/video-only.mp4 \
    -map 1:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -copyts -t "$TOPOLOGY_DURATION" -avoid_negative_ts disabled \
    -metadata creation_time=1970-01-01T00:00:00Z \
    -movflags +faststart topology/delayed-video.mp4

run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$HLS_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -f hls -hls_time "$HLS_SEGMENT_SECONDS" -hls_list_size 0 \
    -hls_playlist_type vod -hls_flags independent_segments \
    -hls_base_url 'segments/' \
    -start_number 0 -hls_segment_filename 'hls/vod/segments/segment-%03d.ts' \
    hls/vod/index.m3u8

run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$HLS_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -f hls -hls_time "$HLS_SEGMENT_SECONDS" -hls_list_size 0 \
    -hls_flags independent_segments+omit_endlist \
    -hls_base_url 'segments/' \
    -start_number 0 -hls_segment_filename 'hls/live/segments/segment-%03d.ts' \
    hls/live/all.m3u8

# Separate live/VOD renditions model players whose master playlist exposes an
# audio group independently from video. This is intentionally distinct from
# the muxed MPEG-TS route because the observed live regression briefly exposed
# audio and then continued as silent video.
for playlist_kind in vod live-separate; do
    if [[ "$playlist_kind" == vod ]]; then
        separate_root=hls/vod-separate
        playlist_flags=independent_segments
        playlist_type=(-hls_playlist_type vod)
    else
        separate_root=hls/live-separate
        playlist_flags=independent_segments+omit_endlist
        playlist_type=()
    fi

    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$HLS_DURATION" \
        -map 0:v:0 -an -map_metadata -1 -map_chapters -1 -c:v copy \
        -f hls -hls_time "$HLS_SEGMENT_SECONDS" -hls_list_size 0 \
        "${playlist_type[@]}" -hls_flags "$playlist_flags" \
        -hls_base_url 'segments/' -start_number 0 \
        -hls_segment_filename "${separate_root}/video/segments/video-%03d.ts" \
        "${separate_root}/video/all.m3u8"

    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$HLS_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 -c:a copy \
        -f hls -hls_time "$HLS_SEGMENT_SECONDS" -hls_list_size 0 \
        "${playlist_type[@]}" -hls_flags "$playlist_flags" \
        -hls_base_url 'segments/' -start_number 0 \
        -hls_segment_filename "${separate_root}/audio/segments/audio-%03d.ts" \
        "${separate_root}/audio/all.m3u8"
done

# Independent audio HLS can end with one encoder-padding fragment while video
# ends on a complete GOP (or vice versa). Such a tail is useful for finite VOD
# EOS testing, but it is not a valid live-window generation. Keep the longest
# common prefix whose final segment is at least 90% of the declared interval.
"$PYTHON_BIN" "$ALIGN_LIVE_RENDITIONS_TOOL" \
    --audio hls/live-separate/audio/all.m3u8 \
    --video hls/live-separate/video/all.m3u8 \
    --segment-seconds "$HLS_SEGMENT_SECONDS"

run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$HLS_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -f hls -hls_time "$HLS_SEGMENT_SECONDS" -hls_list_size 0 \
    -hls_playlist_type vod -hls_flags independent_segments \
    -hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 \
    -hls_base_url 'segments/' -start_number 0 \
    -hls_segment_filename 'hls/cmaf/segments/segment-%03d.m4s' \
    hls/cmaf/index.m3u8

"$PYTHON_BIN" "$LIVE_SCHEDULE_TOOL" \
    --source hls/live/all.m3u8 \
    --output hls/live \
    --window-size "$HLS_LIVE_WINDOW" \
    --interval-seconds "$HLS_SEGMENT_SECONDS"

for track in video audio; do
    "$PYTHON_BIN" "$LIVE_SCHEDULE_TOOL" \
        --source "hls/live-separate/${track}/all.m3u8" \
        --output "hls/live-separate/${track}" \
        --window-size "$HLS_LIVE_WINDOW" \
        --interval-seconds "$HLS_SEGMENT_SECONDS"
    cp "hls/live-separate/${track}/window-0000.m3u8" \
        "hls/live-separate/${track}/index.m3u8"
done

"$PYTHON_BIN" "$PAIRED_LIVE_SCHEDULE_TOOL" \
    --audio hls/live-separate/audio/schedule.json \
    --video hls/live-separate/video/schedule.json \
    --output hls/live-separate/schedule.json

for master_root in hls/vod-separate hls/live-separate; do
    if [[ "$master_root" == hls/vod-separate ]]; then
        cp "$master_root/video/all.m3u8" "$master_root/video/index.m3u8"
        cp "$master_root/audio/all.m3u8" "$master_root/audio/index.m3u8"
    fi
    printf '%s\n' \
        '#EXTM3U' \
        '#EXT-X-VERSION:6' \
        '#EXT-X-INDEPENDENT-SEGMENTS' \
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="Stereo",DEFAULT=YES,AUTOSELECT=YES,URI="audio/index.m3u8"' \
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=${WIDTH}x${HEIGHT},FRAME-RATE=${FPS},AUDIO=\"stereo\"" \
        'video/index.m3u8' \
        >"$master_root/master.m3u8"
done

# Reference-discovery corpus. Same-track remuxes isolate containers where the
# container permits H.264/AAC; format-native pairs are named by both codecs so
# they are never mistaken for a container-only comparison.
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -movflags +faststart discovery/containers/h264-aac.mp4
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    discovery/containers/h264-aac.mov
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    discovery/containers/h264-aac.mkv
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 -c copy \
    -f mpegts discovery/containers/h264-aac.ts

run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v libx265 -preset medium -pix_fmt yuv420p -threads:v 1 \
    -x265-params 'pools=none:frame-threads=1:wpp=0:log-level=error' \
    -c:a aac -b:a 128k -tag:v hvc1 discovery/codecs/hevc-aac.mp4
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v libvpx -deadline good -cpu-used 4 -threads:v 1 -b:v 800k \
    -c:a libvorbis -q:a 4 discovery/codecs/vp8-vorbis.webm
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v libvpx-vp9 -deadline good -cpu-used 5 -row-mt 0 -threads:v 1 -b:v 800k \
    -c:a libopus -b:a 128k discovery/codecs/vp9-opus.webm
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v mpeg4 -q:v 5 -threads:v 1 -c:a libmp3lame -b:a 160k \
    discovery/codecs/mpeg4-mp3.avi
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v wmv2 -q:v 5 -threads:v 1 -c:a wmav2 -b:a 160k \
    discovery/codecs/wmv2-wmav2.asf
run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
    -map 0:v:0 -map 0:a:0 -map_metadata -1 -map_chapters -1 \
    -c:v libaom-av1 -cpu-used 6 -row-mt 0 -tiles 1x1 -threads:v 1 -b:v 800k \
    -c:a libopus -b:a 128k discovery/codecs/av1-opus.webm

for sample_rate in 44100 48000; do
    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
        -ar "$sample_rate" -ac 2 -c:a libmp3lame -b:a 160k \
        "discovery/audio/mp3-${sample_rate}.mp3"
    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
        -ar "$sample_rate" -ac 2 -c:a aac -b:a 128k -f adts \
        "discovery/audio/aac-adts-${sample_rate}.aac"
    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
        -ar "$sample_rate" -ac 2 -c:a pcm_s16le \
        "discovery/audio/pcm-s16-${sample_rate}.wav"
    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
        -ar "$sample_rate" -ac 2 -c:a flac \
        "discovery/audio/flac-${sample_rate}.flac"
    run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
        -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
        -ar "$sample_rate" -ac 2 -c:a libvorbis -q:a 4 \
        "discovery/audio/vorbis-${sample_rate}.ogg"
    if [[ "$sample_rate" == 48000 ]]; then
        run_ffmpeg "${common[@]}" -i av/faststart.mp4 -t "$MATRIX_DURATION" \
            -map 0:a:0 -vn -map_metadata -1 -map_chapters -1 \
            -ar "$sample_rate" -ac 2 -c:a libopus -b:a 128k \
            "discovery/audio/opus-${sample_rate}.opus"
    fi
done

surround_source="aevalsrc=exprs='0.03*sin(2*PI*220*t)|0.03*sin(2*PI*330*t)|0.03*sin(2*PI*440*t)|0.03*sin(2*PI*55*t)|0.03*sin(2*PI*660*t)|0.03*sin(2*PI*880*t)':s=${AUDIO_RATE}:d=${MATRIX_DURATION}:c=5.1"
run_ffmpeg "${common[@]}" -f lavfi -i "$surround_source" \
    -map 0:a:0 -map_metadata -1 -map_chapters -1 -c:a aac -b:a 384k \
    discovery/audio/aac-5.1.m4a

run_ffmpeg "${common[@]}" -i av/faststart.mp4 \
    -f lavfi -i "sine=frequency=997:sample_rate=${AUDIO_RATE}:duration=${MATRIX_DURATION}" \
    -t "$MATRIX_DURATION" -map 0:v:0 -map 0:a:0 -map 1:a:0 \
    -map_metadata -1 -map_chapters -1 -c:v copy -c:a aac -b:a 128k -ac 2 \
    -metadata:s:a:0 language=eng -metadata:s:a:0 title=Primary \
    -metadata:s:a:1 language=jpn -metadata:s:a:1 title=Alternate \
    -disposition:a:0 default -disposition:a:1 0 -movflags +faststart \
    discovery/audio/two-audio-tracks.mp4

printf 'This is deterministic non-media input for parser rejection.\n' \
    >invalid/not-media.bin
: >invalid/empty.bin

faststart_size=$(stat -c '%s' av/faststart.mp4)
tail_size=$(stat -c '%s' av/tail-moov.mp4)
faststart_cut=$((faststart_size * 55 / 100))
tail_cut=$((tail_size * 55 / 100))
head -c "$faststart_cut" av/faststart.mp4 >invalid/truncated-faststart.mp4
head -c "$tail_cut" av/tail-moov.mp4 >invalid/truncated-tail.mp4

printf '%s\n' \
    '#EXTM3U' \
    '#EXT-X-VERSION:3' \
    '#EXT-X-TARGETDURATION:2' \
    '#EXT-X-MEDIA-SEQUENCE:0' \
    '#EXTINF:2.000000,' \
    'segment-that-does-not-exist.ts' \
    '#EXT-X-ENDLIST' \
    >invalid/missing-segment.m3u8

printf '%s\n' \
    '#EXTM3U' \
    '#EXT-X-TARGETDURATION:not-a-number' \
    '#EXTINF:not-a-duration' \
    >invalid/malformed.m3u8

# This intermediate is deliberately omitted from the distributable fixture
# tree.  Every retained member is independently hashed and probed later.
rm -f provenance/base.mkv
