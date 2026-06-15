#!/usr/bin/env swift

import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

struct Options {
    var spritesheet: URL?
    var outDir: URL?
    var id: String?
    var displayName: String?
    var author: String?
    var frameWidth: Int = 192
    var frameHeight: Int = 208
    var force: Bool = false
}

struct StateSpec {
    let id: String
    let row: Int
    let frames: Int
    let fps: Int
}

let states: [StateSpec] = [
    StateSpec(id: "idle", row: 0, frames: 6, fps: 4),
    StateSpec(id: "running-right", row: 1, frames: 8, fps: 8),
    StateSpec(id: "running-left", row: 2, frames: 8, fps: 8),
    StateSpec(id: "waving", row: 3, frames: 4, fps: 6),
    StateSpec(id: "jumping", row: 4, frames: 5, fps: 7),
    StateSpec(id: "failed", row: 5, frames: 8, fps: 7),
    StateSpec(id: "waiting", row: 6, frames: 6, fps: 6),
    StateSpec(id: "running", row: 7, frames: 6, fps: 8),
    StateSpec(id: "review", row: 8, frames: 6, fps: 6),
]

func usage() -> Never {
    print("""
    Usage:
      swift scripts/import_petdex_spritesheet.swift \\
        --spritesheet path/to/spritesheet.webp \\
        --out-dir assets/packs/my_pet \\
        --id my_pet \\
        --display-name "My Pet" \\
        [--author "Name"] [--frame-width 192] [--frame-height 208] [--force]

    Imports the OpenPets/Petdex 8x9 universal sprite layout into a Deskmate
    character pack directory. Output frames are PNG files plus manifest.json.
    """)
    exit(2)
}

func die(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func parseArgs(_ args: [String]) -> Options {
    var options = Options()
    var i = 0
    func value(after flag: String) -> String {
        guard i + 1 < args.count else { die("missing value after \(flag)") }
        i += 1
        return args[i]
    }

    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--help", "-h":
            usage()
        case "--spritesheet":
            options.spritesheet = URL(fileURLWithPath: value(after: arg)).standardizedFileURL
        case "--out-dir":
            options.outDir = URL(fileURLWithPath: value(after: arg)).standardizedFileURL
        case "--id":
            options.id = value(after: arg)
        case "--display-name":
            options.displayName = value(after: arg)
        case "--author":
            options.author = value(after: arg)
        case "--frame-width":
            guard let parsed = Int(value(after: arg)), parsed > 0 else {
                die("--frame-width must be a positive integer")
            }
            options.frameWidth = parsed
        case "--frame-height":
            guard let parsed = Int(value(after: arg)), parsed > 0 else {
                die("--frame-height must be a positive integer")
            }
            options.frameHeight = parsed
        case "--force":
            options.force = true
        default:
            die("unknown argument: \(arg)")
        }
        i += 1
    }
    return options
}

func safePackId(_ id: String) -> String {
    let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    let scalars = id.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }
    let cleaned = String(scalars).trimmingCharacters(in: CharacterSet(charactersIn: "-_"))
    return cleaned.isEmpty ? "imported_pet" : cleaned
}

func loadImage(_ url: URL) -> CGImage {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        die("failed to decode spritesheet: \(url.path)")
    }
    return image
}

func writePNG(_ image: CGImage, to url: URL) {
    guard let destination = CGImageDestinationCreateWithURL(
        url as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        die("failed to create PNG destination: \(url.path)")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        die("failed to write PNG: \(url.path)")
    }
}

let options = parseArgs(Array(CommandLine.arguments.dropFirst()))
guard let spritesheetURL = options.spritesheet else { die("missing --spritesheet") }
guard let outDir = options.outDir else { die("missing --out-dir") }
guard let rawId = options.id else { die("missing --id") }
guard let displayName = options.displayName else { die("missing --display-name") }

let packId = safePackId(rawId)
let fileManager = FileManager.default
let manifestURL = outDir.appendingPathComponent("manifest.json")
if fileManager.fileExists(atPath: manifestURL.path) && !options.force {
    die("manifest already exists at \(manifestURL.path); pass --force to overwrite")
}

let sheet = loadImage(spritesheetURL)
let expectedWidth = options.frameWidth * 8
let expectedHeight = options.frameHeight * 9
guard sheet.width >= expectedWidth, sheet.height >= expectedHeight else {
    die(
        "spritesheet is \(sheet.width)x\(sheet.height), expected at least "
        + "\(expectedWidth)x\(expectedHeight)"
    )
}

do {
    try fileManager.createDirectory(at: outDir, withIntermediateDirectories: true)
} catch {
    die("failed to create output directory: \(error.localizedDescription)")
}

var stateManifest: [String: Any] = [:]
for state in states {
    let stateDir = outDir.appendingPathComponent(state.id, isDirectory: true)
    do {
        try fileManager.createDirectory(at: stateDir, withIntermediateDirectories: true)
    } catch {
        die("failed to create state directory \(stateDir.path): \(error.localizedDescription)")
    }

    var framePaths: [String] = []
    for col in 0..<state.frames {
        let crop = CGRect(
            x: col * options.frameWidth,
            y: state.row * options.frameHeight,
            width: options.frameWidth,
            height: options.frameHeight
        )
        guard let frame = sheet.cropping(to: crop) else {
            die("failed to crop \(state.id) frame \(col)")
        }
        let fileName = String(format: "%03d.png", col)
        let relative = "\(state.id)/\(fileName)"
        writePNG(frame, to: stateDir.appendingPathComponent(fileName))
        framePaths.append(relative)
    }
    stateManifest[state.id] = [
        "fps": state.fps,
        "frames": framePaths,
    ]
}

let manifest: [String: Any] = [
    "spec_version": 1,
    "id": packId,
    "display_name": displayName,
    "author": options.author ?? "Imported",
    "canvas_size": [options.frameWidth, options.frameHeight],
    "scale": 1,
    "palette": [],
    "avatar": [
        "default_style": "pixel",
        "supported_styles": ["pixel"],
    ],
    "states": stateManifest,
    "required_states": ["idle", "running", "review", "waiting", "waving", "jumping", "failed"],
    "fallbacks": [
        "working": "running",
        "thinking": "review",
        "alert": "waiting",
        "happy": "jumping",
        "dozing": "idle",
        "sleeping": "idle",
        "waking": "jumping",
        "drag": "running",
        "react-click": "waving",
        "petting": "waving",
        "editing": "running",
        "testing": "waiting",
        "success": "jumping",
        "error": "failed",
        "celebrating": "jumping",
        "notification": "waving",
        "walking": "running",
        "walking_left": "running-left",
        "walking_right": "running-right",
        "running_left": "running-left",
        "running_right": "running-right",
    ],
    "capabilities": ["universal_sprite_states", "direct_reactions", "auto_rest"],
]

do {
    let data = try JSONSerialization.data(
        withJSONObject: manifest,
        options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    )
    try data.write(to: manifestURL)
} catch {
    die("failed to write manifest: \(error.localizedDescription)")
}

print("Imported \(packId) -> \(outDir.path)")
