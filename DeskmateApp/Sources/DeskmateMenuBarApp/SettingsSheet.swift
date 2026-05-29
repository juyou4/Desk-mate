import SwiftUI
import DeskmateCore
import CoreGraphics

/// Settings sheet for TopSurfaceCustomization (R8).
struct SettingsSheet: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    @State private var theme: String = "system"
    @State private var fontScale: Double = 1.0
    @State private var buddyStyle: String = "pixel"
    @State private var showBuddy: Bool = true
    @State private var hardwareNotchMode: HardwareNotchMode = .automatic
    @State private var hoverSpeed: Double = 1.0
    @State private var feedbackAudio: Bool = false
    @State private var feedbackAudioName: String = ""
    @State private var preferredScreenId: String = ""
    @State private var accent: IslandAccent = .system
    @State private var useGradientProgress: Bool = true
    @State private var fieldError: String? = nil

    // Coalesce timer
    @State private var coalesceWorkItem: DispatchWorkItem? = nil

    var body: some View {
        Form {
            Section("Appearance") {
                Picker("Theme", selection: $theme) {
                    Text("System").tag("system")
                    Text("Light").tag("light")
                    Text("Dark").tag("dark")
                }
                .onChange(of: theme) { _ in scheduleApply() }

                HStack {
                    Text("Font Scale")
                    Slider(value: $fontScale, in: 0.5...2.0, step: 0.1)
                    Text(String(format: "%.1f", fontScale))
                        .monospacedDigit()
                }
                .onChange(of: fontScale) { _ in scheduleApply() }
            }

            Section("Buddy") {
                Toggle("Show Buddy", isOn: $showBuddy)
                    .onChange(of: showBuddy) { _ in scheduleApply() }
                Picker("Style", selection: $buddyStyle) {
                    Text("Pixel").tag("pixel")
                    Text("Emoji").tag("emoji")
                }
                .onChange(of: buddyStyle) { _ in scheduleApply() }
            }

            Section("Island") {
                Picker("Notch Mode", selection: $hardwareNotchMode) {
                    Text("Automatic").tag(HardwareNotchMode.automatic)
                    Text("Force Notched").tag(HardwareNotchMode.forceNotched)
                    Text("Force Flat").tag(HardwareNotchMode.forceFlat)
                }
                .onChange(of: hardwareNotchMode) { _ in scheduleApply() }

                HStack {
                    Text("Hover Speed")
                    Slider(value: $hoverSpeed, in: 0.25...4.0, step: 0.25)
                    Text(String(format: "%.2f", hoverSpeed))
                        .monospacedDigit()
                }
                .onChange(of: hoverSpeed) { _ in scheduleApply() }

                Picker("Target Screen", selection: $preferredScreenId) {
                    Text("Automatic").tag("")
                    ForEach(availableScreens, id: \.id) { screen in
                        Text(screen.name).tag(screen.id)
                    }
                }
                .onChange(of: preferredScreenId) { _ in scheduleApply() }

                // V10 polish #7: accent color preset for the chip
                // and progress capsule.
                Picker("Accent", selection: $accent) {
                    Text("System").tag(IslandAccent.system)
                    Text("Blue").tag(IslandAccent.blue)
                    Text("Purple").tag(IslandAccent.purple)
                    Text("Pink").tag(IslandAccent.pink)
                    Text("Orange").tag(IslandAccent.orange)
                    Text("Green").tag(IslandAccent.green)
                    Text("Mint").tag(IslandAccent.mint)
                    Text("Red").tag(IslandAccent.red)
                }
                .onChange(of: accent) { _ in scheduleApply() }

                // V10 polish #8: gradient toggle for the progress capsule.
                Toggle("Gradient Progress", isOn: $useGradientProgress)
                    .onChange(of: useGradientProgress) { _ in scheduleApply() }
            }

            Section("Feedback") {
                Toggle("Audio Feedback", isOn: $feedbackAudio)
                    .onChange(of: feedbackAudio) { _ in scheduleApply() }
                if feedbackAudio {
                    TextField("Sound Name", text: $feedbackAudioName)
                        .onChange(of: feedbackAudioName) { _ in scheduleApply() }
                }
            }

            if let error = fieldError {
                Section {
                    Text(error)
                        .foregroundStyle(.red)
                        .font(.caption)
                }
            }

            Section {
                HStack {
                    Text("Schema Version")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("\(runtime.topSurfaceCustomization.current.specVersion)")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 320, height: 480)
        .onAppear { loadFromStore() }
    }

    private func loadFromStore() {
        let current = runtime.topSurfaceCustomization.current
        theme = current.theme
        fontScale = current.fontScale
        buddyStyle = current.buddyStyle
        showBuddy = current.showBuddy
        hardwareNotchMode = current.hardwareNotchMode
        hoverSpeed = current.hoverSpeed
        feedbackAudio = current.feedback.audio
        feedbackAudioName = current.feedback.audioName ?? ""
        preferredScreenId = current.preferredScreenId ?? ""
        accent = current.accent
        useGradientProgress = current.useGradientProgress
    }

    private func scheduleApply() {
        coalesceWorkItem?.cancel()
        let item = DispatchWorkItem { [self] in
            applyToStore()
        }
        coalesceWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: item)
    }

    private func applyToStore() {
        // Validate ranges
        guard fontScale >= 0.5, fontScale <= 2.0 else {
            fieldError = "Font scale must be between 0.5 and 2.0"
            return
        }
        guard hoverSpeed >= 0.25, hoverSpeed <= 4.0 else {
            fieldError = "Hover speed must be between 0.25 and 4.0"
            return
        }
        fieldError = nil

        var updated = runtime.topSurfaceCustomization.current
        updated.theme = theme
        updated.fontScale = fontScale
        updated.buddyStyle = buddyStyle
        updated.showBuddy = showBuddy
        updated.hardwareNotchMode = hardwareNotchMode
        updated.hoverSpeed = hoverSpeed
        updated.feedback = FeedbackPrefs(
            audio: feedbackAudio,
            audioName: feedbackAudioName.isEmpty ? nil : feedbackAudioName
        )
        updated.preferredScreenId = preferredScreenId.isEmpty ? nil : preferredScreenId
        updated.accent = accent
        updated.useGradientProgress = useGradientProgress
        runtime.topSurfaceCustomization.apply(updated)
    }

    // MARK: - Screen Picker Helpers

    private struct ScreenOption: Identifiable {
        let id: String
        let name: String
    }

    private var availableScreens: [ScreenOption] {
        #if canImport(AppKit)
        return NSScreen.screens.compactMap { screen in
            guard let screenNumber = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID else {
                return nil
            }
            guard let uuid = CGDisplayCreateUUIDFromDisplayID(screenNumber) else {
                return nil
            }
            let id = CFUUIDCreateString(nil, uuid.takeUnretainedValue()) as String
            let name = screen.localizedName
            return ScreenOption(id: id, name: name)
        }
        #else
        return []
        #endif
    }
}
