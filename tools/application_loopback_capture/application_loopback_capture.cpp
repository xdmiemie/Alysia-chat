#include <Windows.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <mmdeviceapi.h>
#include <wrl/client.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;

#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

static std::atomic<bool> g_stop_requested{false};

static void on_signal(int) {
    g_stop_requested.store(true);
}

static bool arg_value(int argc, char** argv, const char* name, std::string& value) {
    for (int index = 1; index < argc - 1; ++index) {
        if (std::strcmp(argv[index], name) == 0) {
            value = argv[index + 1];
            return true;
        }
    }
    return false;
}

static bool has_flag(int argc, char** argv, const char* name) {
    for (int index = 1; index < argc; ++index) {
        if (std::strcmp(argv[index], name) == 0) {
            return true;
        }
    }
    return false;
}

static void print_usage() {
    std::cerr
        << "Usage: application_loopback_capture.exe --pid <pid> --out <captured.wav> "
        << "[--seconds 10] [--sample-rate 48000] [--channels 2] [--exclude]\n";
}

static std::string hresult_text(HRESULT hr) {
    char* message = nullptr;
    FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr,
        static_cast<DWORD>(hr),
        MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<LPSTR>(&message),
        0,
        nullptr
    );
    std::string text = message ? message : "";
    if (message) {
        LocalFree(message);
    }
    while (!text.empty() && (text.back() == '\r' || text.back() == '\n')) {
        text.pop_back();
    }
    char code[32];
    std::snprintf(code, sizeof(code), "0x%08lx", static_cast<unsigned long>(hr));
    return std::string(code) + (text.empty() ? "" : (" " + text));
}

class ActivationHandler final : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
    ActivationHandler() {
        event_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }

    ~ActivationHandler() {
        if (event_) {
            CloseHandle(event_);
        }
    }

    HRESULT wait(DWORD timeout_ms) {
        DWORD result = WaitForSingleObject(event_, timeout_ms);
        if (result == WAIT_OBJECT_0) {
            return S_OK;
        }
        return result == WAIT_TIMEOUT ? HRESULT_FROM_WIN32(ERROR_TIMEOUT) : HRESULT_FROM_WIN32(GetLastError());
    }

    HRESULT result() const {
        return activate_result_;
    }

    IAudioClient* audio_client() const {
        return audio_client_.Get();
    }

    ULONG STDMETHODCALLTYPE AddRef() override {
        return InterlockedIncrement(&ref_count_);
    }

    ULONG STDMETHODCALLTYPE Release() override {
        ULONG count = InterlockedDecrement(&ref_count_);
        if (count == 0) {
            delete this;
        }
        return count;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *object = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == __uuidof(IAgileObject)) {
            *object = static_cast<IAgileObject*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }

    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation* operation) override {
        HRESULT operation_result = E_FAIL;
        ComPtr<IUnknown> activated;
        HRESULT hr = operation->GetActivateResult(&operation_result, activated.GetAddressOf());
        activate_result_ = FAILED(hr) ? hr : operation_result;
        if (SUCCEEDED(activate_result_) && activated) {
            activate_result_ = activated.As(&audio_client_);
        }
        SetEvent(event_);
        return S_OK;
    }

private:
    volatile long ref_count_ = 1;
    HANDLE event_ = nullptr;
    HRESULT activate_result_ = E_FAIL;
    ComPtr<IAudioClient> audio_client_;
};

struct WavWriter {
    std::ofstream stream;
    uint16_t channels = 0;
    uint32_t sample_rate = 0;
    uint32_t data_bytes = 0;

    bool open(const std::string& path, uint16_t channel_count, uint32_t rate) {
        channels = channel_count;
        sample_rate = rate;
        data_bytes = 0;
        stream.open(path, std::ios::binary | std::ios::trunc);
        if (!stream) {
            return false;
        }
        write_header(0);
        return true;
    }

    void write_bytes(const void* data, uint32_t bytes) {
        if (bytes == 0) {
            return;
        }
        stream.write(reinterpret_cast<const char*>(data), bytes);
        // Stage 17 tails the growing PCM payload so VAD can finish a turn
        // without waiting for the whole call capture to close.
        stream.flush();
        data_bytes += bytes;
    }

    void close() {
        if (!stream) {
            return;
        }
        stream.seekp(0, std::ios::beg);
        write_header(data_bytes);
        stream.close();
    }

private:
    template <typename T>
    void write_value(T value) {
        stream.write(reinterpret_cast<const char*>(&value), sizeof(T));
    }

    void write_header(uint32_t data_size) {
        const uint32_t byte_rate = sample_rate * channels * 2;
        const uint16_t block_align = channels * 2;
        stream.write("RIFF", 4);
        write_value<uint32_t>(36 + data_size);
        stream.write("WAVE", 4);
        stream.write("fmt ", 4);
        write_value<uint32_t>(16);
        write_value<uint16_t>(1);
        write_value<uint16_t>(channels);
        write_value<uint32_t>(sample_rate);
        write_value<uint32_t>(byte_rate);
        write_value<uint16_t>(block_align);
        write_value<uint16_t>(16);
        stream.write("data", 4);
        write_value<uint32_t>(data_size);
    }
};

static HRESULT activate_process_loopback(DWORD pid, bool exclude, IAudioClient** audio_client) {
    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode =
        exclude ? PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
                : PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;

    PROPVARIANT prop;
    PropVariantInit(&prop);
    prop.vt = VT_BLOB;
    prop.blob.cbSize = sizeof(params);
    prop.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    ActivationHandler* raw_handler = new ActivationHandler();
    ComPtr<ActivationHandler> handler;
    handler.Attach(raw_handler);
    ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
    HRESULT hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &prop,
        handler.Get(),
        operation.GetAddressOf()
    );
    if (FAILED(hr)) {
        return hr;
    }
    hr = handler->wait(10000);
    if (FAILED(hr)) {
        return hr;
    }
    hr = handler->result();
    if (FAILED(hr)) {
        return hr;
    }
    return handler->audio_client()->QueryInterface(__uuidof(IAudioClient), reinterpret_cast<void**>(audio_client));
}

int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    std::string pid_text;
    std::string out_path;
    std::string seconds_text = "10";
    std::string sample_rate_text = "48000";
    std::string channels_text = "2";

    if (!arg_value(argc, argv, "--pid", pid_text) || !arg_value(argc, argv, "--out", out_path)) {
        print_usage();
        return 2;
    }
    arg_value(argc, argv, "--seconds", seconds_text);
    arg_value(argc, argv, "--sample-rate", sample_rate_text);
    arg_value(argc, argv, "--channels", channels_text);

    const auto pid = static_cast<DWORD>(std::strtoul(pid_text.c_str(), nullptr, 10));
    const auto seconds = std::strtod(seconds_text.c_str(), nullptr);
    const auto sample_rate = static_cast<DWORD>(std::strtoul(sample_rate_text.c_str(), nullptr, 10));
    const auto channels = static_cast<WORD>(std::strtoul(channels_text.c_str(), nullptr, 10));
    const bool exclude = has_flag(argc, argv, "--exclude");

    if (pid == 0 || out_path.empty() || seconds <= 0 || sample_rate == 0 || channels == 0) {
        print_usage();
        return 2;
    }

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::cerr << "CoInitializeEx failed: " << hresult_text(hr) << "\n";
        return 3;
    }

    ComPtr<IAudioClient> audio_client;
    hr = activate_process_loopback(pid, exclude, audio_client.GetAddressOf());
    if (FAILED(hr)) {
        std::cerr << "ActivateAudioInterfaceAsync process loopback failed: " << hresult_text(hr) << "\n";
        CoUninitialize();
        return 4;
    }

    WAVEFORMATEX format{};
    format.wFormatTag = WAVE_FORMAT_PCM;
    format.nChannels = channels;
    format.nSamplesPerSec = sample_rate;
    format.wBitsPerSample = 16;
    format.nBlockAlign = static_cast<WORD>(format.nChannels * format.wBitsPerSample / 8);
    format.nAvgBytesPerSec = format.nSamplesPerSec * format.nBlockAlign;

    REFERENCE_TIME buffer_duration = 10000000;
    hr = audio_client->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        buffer_duration,
        0,
        &format,
        nullptr
    );
    if (FAILED(hr)) {
        std::cerr << "IAudioClient::Initialize failed: " << hresult_text(hr) << "\n";
        CoUninitialize();
        return 5;
    }

    HANDLE sample_ready = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!sample_ready) {
        std::cerr << "CreateEvent failed: " << GetLastError() << "\n";
        CoUninitialize();
        return 6;
    }
    hr = audio_client->SetEventHandle(sample_ready);
    if (FAILED(hr)) {
        std::cerr << "SetEventHandle failed: " << hresult_text(hr) << "\n";
        CloseHandle(sample_ready);
        CoUninitialize();
        return 7;
    }

    ComPtr<IAudioCaptureClient> capture_client;
    hr = audio_client->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void**>(capture_client.GetAddressOf()));
    if (FAILED(hr)) {
        std::cerr << "GetService(IAudioCaptureClient) failed: " << hresult_text(hr) << "\n";
        CloseHandle(sample_ready);
        CoUninitialize();
        return 8;
    }

    WavWriter writer;
    if (!writer.open(out_path, channels, sample_rate)) {
        std::cerr << "Failed to open output WAV: " << out_path << "\n";
        CloseHandle(sample_ready);
        CoUninitialize();
        return 9;
    }

    hr = audio_client->Start();
    if (FAILED(hr)) {
        std::cerr << "IAudioClient::Start failed: " << hresult_text(hr) << "\n";
        writer.close();
        CloseHandle(sample_ready);
        CoUninitialize();
        return 10;
    }

    std::cout << "READY pid=" << pid << " out=" << out_path << "\n";
    std::cout.flush();

    uint64_t frames_written = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(
        static_cast<long long>(seconds * 1000.0)
    );
    std::vector<uint8_t> silence;
    while (!g_stop_requested.load() && std::chrono::steady_clock::now() < deadline) {
        WaitForSingleObject(sample_ready, 100);
        UINT32 packet_frames = 0;
        hr = capture_client->GetNextPacketSize(&packet_frames);
        if (FAILED(hr)) {
            std::cerr << "GetNextPacketSize failed: " << hresult_text(hr) << "\n";
            break;
        }
        while (packet_frames > 0) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            hr = capture_client->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
            if (FAILED(hr)) {
                std::cerr << "GetBuffer failed: " << hresult_text(hr) << "\n";
                break;
            }
            const uint32_t bytes = frames * format.nBlockAlign;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silence.assign(bytes, 0);
                writer.write_bytes(silence.data(), bytes);
            } else {
                writer.write_bytes(data, bytes);
            }
            frames_written += frames;
            capture_client->ReleaseBuffer(frames);
            hr = capture_client->GetNextPacketSize(&packet_frames);
            if (FAILED(hr)) {
                std::cerr << "GetNextPacketSize failed: " << hresult_text(hr) << "\n";
                packet_frames = 0;
            }
        }
    }

    audio_client->Stop();
    writer.close();
    CloseHandle(sample_ready);
    CoUninitialize();

    std::cout << "DONE frames=" << frames_written << "\n";
    std::cout.flush();
    return 0;
}
