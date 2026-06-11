#!/usr/bin/env swift

import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

struct Options {
    var sourceDir: URL?
    var outDir: URL?
    var id: String?
    var displayName: String?
    var author: String?
    var hueShift: Double?
    var force = false
}

func usage() -> Never {
    print("""
    Usage:
      swift scripts/recolor_character_pack.swift \\
        --source-dir assets/packs/openpets_default \\
        --out-dir assets/packs/openpets_aqua \\
        --id openpets_aqua \\
        --display-name "OpenPets Aqua" \\
        --hue-shift 170 \\
        [--author "Name"] [--force]

    Recolors PNG frames in a manifest-backed character pack while preserving
    alpha, outlines, frame layout, and manifest animation timing.
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
        case "--source-dir":
            options.sourceDir = URL(fileURLWithPath: value(after: arg)).standardizedFileURL
        case "--out-dir":
            options.outDir = URL(fileURLWithPath: value(after: arg)).standardizedFileURL
        case "--id":
            options.id = value(after: arg)
        case "--display-name":
            options.displayName = value(after: arg)
        case "--author":
            options.author = value(after: arg)
        case "--hue-shift":
            guard let parsed = Double(value(after: arg)) else {
                die("--hue-shift must be a number")
            }
            options.hueShift = parsed
        case "--force":
            options.force = true
        default:
            die("unknown argument: \(arg)")
        }
        i += 1
    }
    return options
}

func copyOrCreateDirectory(_ url: URL) {
    do {
        try FileManager.default.createDirectory(
            at: url,
            withIntermediateDirectories: true
        )
    } catch {
        die("failed to create \(url.path): \(error.localizedDescription)")
    }
}

func loadPNG(_ url: URL) -> CGImage {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        die("failed to decode image: \(url.path)")
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

func hueShifted(_ image: CGImage, degrees: Double) -> CGImage {
    let width = image.width
    let height = image.height
    let bytesPerPixel = 4
    let bytesPerRow = width * bytesPerPixel
    var data = [UInt8](repeating: 0, count: height * bytesPerRow)
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(
            data: &data,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    else {
        die("failed to allocate image buffer")
    }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

    for y in 0..<height {
        for x in 0..<width {
            let offset = y * bytesPerRow + x * bytesPerPixel
            let alpha = data[offset + 3]
            guard alpha > 0 else { continue }

            let r = Double(data[offset]) / 255.0
            let g = Double(data[offset + 1]) / 255.0
            let b = Double(data[offset + 2]) / 255.0
            var hsl = rgbToHsl(r: r, g: g, b: b)
            if hsl.s > 0.12 && hsl.l > 0.12 {
                hsl.h = wrapHue(hsl.h + degrees / 360.0)
                let shifted = hslToRgb(h: hsl.h, s: hsl.s, l: hsl.l)
                data[offset] = UInt8(clamping: Int((shifted.r * 255.0).rounded()))
                data[offset + 1] = UInt8(clamping: Int((shifted.g * 255.0).rounded()))
                data[offset + 2] = UInt8(clamping: Int((shifted.b * 255.0).rounded()))
            }
        }
    }

    guard let output = context.makeImage() else {
        die("failed to build recolored image")
    }
    return output
}

func wrapHue(_ value: Double) -> Double {
    var h = value.truncatingRemainder(dividingBy: 1.0)
    if h < 0 { h += 1.0 }
    return h
}

func rgbToHsl(r: Double, g: Double, b: Double) -> (h: Double, s: Double, l: Double) {
    let maxValue = max(r, g, b)
    let minValue = min(r, g, b)
    let l = (maxValue + minValue) / 2.0
    let delta = maxValue - minValue
    guard delta > 0 else { return (0, 0, l) }

    let s = delta / (1.0 - abs(2.0 * l - 1.0))
    let h: Double
    if maxValue == r {
        h = ((g - b) / delta).truncatingRemainder(dividingBy: 6.0) / 6.0
    } else if maxValue == g {
        h = (((b - r) / delta) + 2.0) / 6.0
    } else {
        h = (((r - g) / delta) + 4.0) / 6.0
    }
    return (wrapHue(h), s, l)
}

func hslToRgb(h: Double, s: Double, l: Double) -> (r: Double, g: Double, b: Double) {
    let c = (1.0 - abs(2.0 * l - 1.0)) * s
    let hp = h * 6.0
    let x = c * (1.0 - abs(hp.truncatingRemainder(dividingBy: 2.0) - 1.0))
    let prime: (Double, Double, Double)
    switch hp {
    case 0..<1: prime = (c, x, 0)
    case 1..<2: prime = (x, c, 0)
    case 2..<3: prime = (0, c, x)
    case 3..<4: prime = (0, x, c)
    case 4..<5: prime = (x, 0, c)
    default: prime = (c, 0, x)
    }
    let m = l - c / 2.0
    return (prime.0 + m, prime.1 + m, prime.2 + m)
}

func rewriteManifest(
    from sourceURL: URL,
    to targetURL: URL,
    id: String,
    displayName: String,
    author: String
) {
    do {
        let data = try Data(contentsOf: sourceURL)
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            die("manifest is not a JSON object: \(sourceURL.path)")
        }
        object["id"] = id
        object["display_name"] = displayName
        object["author"] = author
        var capabilities = object["capabilities"] as? [String] ?? []
        if !capabilities.contains("recolored_variant") {
            capabilities.append("recolored_variant")
        }
        object["capabilities"] = capabilities
        let out = try JSONSerialization.data(
            withJSONObject: object,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )
        try out.write(to: targetURL)
    } catch {
        die("failed to rewrite manifest: \(error.localizedDescription)")
    }
}

let options = parseArgs(Array(CommandLine.arguments.dropFirst()))
guard let sourceDir = options.sourceDir else { die("missing --source-dir") }
guard let outDir = options.outDir else { die("missing --out-dir") }
guard let id = options.id else { die("missing --id") }
guard let displayName = options.displayName else { die("missing --display-name") }
guard let hueShift = options.hueShift else { die("missing --hue-shift") }
let author = options.author ?? "Deskmate"

let fileManager = FileManager.default
let manifestURL = sourceDir.appendingPathComponent("manifest.json")
guard fileManager.fileExists(atPath: manifestURL.path) else {
    die("source manifest missing: \(manifestURL.path)")
}
if fileManager.fileExists(atPath: outDir.path) {
    guard options.force else {
        die("output exists at \(outDir.path); pass --force to overwrite")
    }
    do {
        try fileManager.removeItem(at: outDir)
    } catch {
        die("failed to clear output directory: \(error.localizedDescription)")
    }
}
copyOrCreateDirectory(outDir)

guard let enumerator = fileManager.enumerator(
    at: sourceDir,
    includingPropertiesForKeys: [.isDirectoryKey],
    options: [.skipsHiddenFiles]
) else {
    die("failed to enumerate \(sourceDir.path)")
}

for case let sourceURL as URL in enumerator {
    let rel = sourceURL.path.replacingOccurrences(
        of: sourceDir.path + "/",
        with: ""
    )
    let targetURL = outDir.appendingPathComponent(rel)
    var isDirectory: ObjCBool = false
    fileManager.fileExists(atPath: sourceURL.path, isDirectory: &isDirectory)
    if isDirectory.boolValue {
        copyOrCreateDirectory(targetURL)
        continue
    }

    copyOrCreateDirectory(targetURL.deletingLastPathComponent())
    if rel == "manifest.json" {
        rewriteManifest(
            from: sourceURL,
            to: targetURL,
            id: id,
            displayName: displayName,
            author: author
        )
    } else if sourceURL.pathExtension.lowercased() == "png" {
        let recolored = hueShifted(loadPNG(sourceURL), degrees: hueShift)
        writePNG(recolored, to: targetURL)
    } else {
        do {
            try fileManager.copyItem(at: sourceURL, to: targetURL)
        } catch {
            die("failed to copy \(rel): \(error.localizedDescription)")
        }
    }
}

print("Recolored \(id) -> \(outDir.path)")
