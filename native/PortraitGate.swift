import Foundation
import ImageIO
import Vision
import UniformTypeIdentifiers
import CoreGraphics

let implementationVersion = "portrait-gate-1.0.1"
let minimumConfidence: Float = 0.75
let minimumFacePixels: CGFloat = 24

func asDictionary(_ value: Any?) -> [String: Any] {
    return value as? [String: Any] ?? [:]
}
func cleanString(_ value: Any?) -> String {
    if let s = value as? String { return s.trimmingCharacters(in: .whitespacesAndNewlines) }
    return ""
}
func positiveNumber(_ value: Any?) -> Double? {
    if let number = value as? NSNumber {
        let n = number.doubleValue
        return n.isFinite && n > 0 ? n : nil
    }
    if let s = value as? String, let n = Double(s), n.isFinite && n > 0 { return n }
    if let values = value as? [Any] {
        for v in values { if let n = positiveNumber(v) { return n } }
    }
    return nil
}
func normalise(_ s: String) -> String {
    return s.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: Locale(identifier: "en_US_POSIX"))
        .lowercased().replacingOccurrences(of: "_", with: " ")
        .replacingOccurrences(of: "-", with: " ")
        .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
}

// Keep the camera's local calendar date. Offset/timezone conversion can change
// the day, and file modification / TIFF DateTime can describe a later edit.
func captureDay(_ value: Any?) -> String? {
    let text = cleanString(value)
    let pattern = #"^([0-9]{4})([:\-])([0-9]{2})\2([0-9]{2})(?:[ T]|$)"#
    guard let expression = try? NSRegularExpression(pattern: pattern),
          let match = expression.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
          let yr = Range(match.range(at: 1), in: text),
          let mr = Range(match.range(at: 3), in: text),
          let dr = Range(match.range(at: 4), in: text),
          let year = Int(text[yr]), let month = Int(text[mr]), let day = Int(text[dr]),
          year >= 1, year <= 9999, month >= 1, month <= 12 else { return nil }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
    let days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    guard day >= 1, day <= days[month - 1] else { return nil }
    return String(format: "%04d-%02d-%02d", year, month, day)
}

func captureMetadata(_ source: CGImageSource, tiff: [String: Any], exif: [String: Any],
                     cameraProvenance: Bool, recognisedRaw: Bool) -> [String: Any] {
    var missing: [String: Any] = ["capture_date": NSNull(), "capture_date_source": NSNull(),
                                 "capture_date_status": "missing_date"]
    func known(_ day: String, source: String, status: String) -> [String: Any] {
        return ["capture_date": day, "capture_date_source": source, "capture_date_status": status]
    }
    if let day = captureDay(exif[kCGImagePropertyExifDateTimeOriginal as String]) {
        return known(day, source: "EXIF.DateTimeOriginal", status: "original")
    }
    let metadata = CGImageSourceCopyMetadataAtIndex(source, 0, nil)
    func metadataString(_ path: String) -> String? {
        guard let metadata = metadata else { return nil }
        return CGImageMetadataCopyStringValueWithPath(metadata, nil, path as CFString) as String?
    }
    if let day = captureDay(metadataString("exif:DateTimeOriginal")) {
        return known(day, source: "XMP.exif:DateTimeOriginal", status: "original")
    }

    // Digitization / generic creation dates are only fallbacks for a camera-
    // native source, never for a known editor/exporter. Missing software does
    // not establish camera-native provenance for an ordinary JPEG/HEIC export.
    let software = normalise(cleanString(tiff[kCGImagePropertyTIFFSoftware as String]))
    let editingMarkers = ["adobe", "photoshop", "lightroom", "capture one", "captureone",
                          "affinity", "snapseed", "gimp", "pixelmator", "canva", "darktable",
                          "rawtherapee", "dxo", "luminar", "photomatix", "imagemagick"]
    let creatorTool = normalise(metadataString("xmp:CreatorTool") ?? "")
    let edited = editingMarkers.contains { software.contains($0) || creatorTool.contains($0) }
    let numericFirmware = !software.isEmpty && software.range(of: #"^[0-9]+(?:\.[0-9]+)+$"#,
                                                             options: .regularExpression) != nil
    let nativeSoftware = numericFirmware || software.contains("digital camera") || software.contains("firmware")
    // ImageIO aliases a stored XMP CreateDate into Exif.DateTimeDigitized and
    // can reconcile CreatorTool with TIFF Software. A stored XMP packet makes
    // those generic creation dates ambiguous, so do not stamp that fallback.
    let storedXMP = ["true", "1", "yes"].contains(normalise(metadataString("iio:hasXMP") ?? ""))
    let cameraNative = cameraProvenance && !edited && !storedXMP && (recognisedRaw || nativeSoftware)
    if cameraNative {
        if let day = captureDay(exif[kCGImagePropertyExifDateTimeDigitized as String]) {
            return known(day, source: "EXIF.DateTimeDigitized", status: "camera_native_fallback")
        }
        if let day = captureDay(metadataString("exif:DateTimeDigitized")) {
            return known(day, source: "XMP.exif:DateTimeDigitized", status: "camera_native_fallback")
        }
        if let day = captureDay(metadataString("xmp:CreateDate")) {
            return known(day, source: "XMP.xmp:CreateDate", status: "camera_native_fallback")
        }
    }
    if captureDay(tiff[kCGImagePropertyTIFFDateTime as String]) != nil {
        missing["capture_date_note"] = "TIFF.DateTime is not proof of capture time; ignored"
    }
    return missing
}
let screenshotMarkers = [
    "screenshot", "screen shot", "screencapture", "screen capture", "screen recording",
    "snipping tool", "snippingtool", "snagit", "greenshot", "cleanshot",
    "截图", "截屏", "截圖", "截圖", "屏幕快照", "屏幕录制", "螢幕快照", "螢幕截圖",
    "スクリーンショット", "画面キャプチャ"
]
func screenshotMatches(_ s: String, location: String) -> [String] {
    let n = normalise(s)
    return screenshotMarkers.compactMap { marker in
        n.contains(normalise(marker)) ? "\(location):\(marker)" : nil
    }
}
func metadataScreenshotFlags(_ value: Any, location: String = "metadata") -> [String] {
    if let s = value as? String { return screenshotMatches(s, location: location) }
    if let dictionary = value as? [String: Any] {
        return dictionary.flatMap { key, value in metadataScreenshotFlags(value, location: "\(location).\(key)") }
    }
    if let array = value as? [Any] { return array.flatMap { metadataScreenshotFlags($0, location: location) } }
    return []
}
func emit(_ result: [String: Any], exitStatus: Int32 = 0) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .withoutEscapingSlashes]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("{\"accepted\":false,\"reason\":\"json_serialization_failed\",\"face_count\":0,\"faces\":[],\"detector_version\":\"\(implementationVersion)\"}")
    }
    exit(exitStatus)
}

var result: [String: Any] = [
    "accepted": false,
    "reason": "uninitialised",
    "face_count": 0,
    "detected_face_count": 0,
    "faces": [[String: Any]](),
    "dimensions": ["width": 0, "height": 0, "thumbnail_width": 0, "thumbnail_height": 0],
    "camera": ["make": "", "model": "", "provenance": false],
    "capture_date": NSNull(),
    "capture_date_source": NSNull(),
    "capture_date_status": "missing_date",
    "screenshot_flags": [String](),
    "minimum_confidence": minimumConfidence,
    "bbox_origin": "top_left_oriented",
    "detector_version": implementationVersion
]
let arguments = CommandLine.arguments
if arguments.count != 2 {
    result["reason"] = "invalid_arguments"
    emit(result, exitStatus: 2)
}
let url = URL(fileURLWithPath: arguments[1])
var flags = screenshotMatches(url.lastPathComponent, location: "filename")
result["screenshot_flags"] = Array(Set(flags)).sorted()
if !flags.isEmpty {
    result["reason"] = "screenshot_marker"
    emit(result)
}
guard let source = CGImageSourceCreateWithURL(url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary) else {
    result["reason"] = "source_unreadable"
    emit(result)
}
let typeIdentifier = (CGImageSourceGetType(source) as String?) ?? ""
let recognisedRaw = UTType(typeIdentifier)?.conforms(to: .rawImage) ?? false
result["image_type"] = typeIdentifier
let properties = asDictionary(CGImageSourceCopyPropertiesAtIndex(source, 0, [kCGImageSourceShouldCache: false] as CFDictionary))
flags += metadataScreenshotFlags(properties)
result["screenshot_flags"] = Array(Set(flags)).sorted()
let tiff = asDictionary(properties[kCGImagePropertyTIFFDictionary as String])
let exif = asDictionary(properties[kCGImagePropertyExifDictionary as String])
let make = cleanString(tiff[kCGImagePropertyTIFFMake as String])
let model = cleanString(tiff[kCGImagePropertyTIFFModel as String])
let exposureTime = positiveNumber(exif[kCGImagePropertyExifExposureTime as String])
let fNumber = positiveNumber(exif[kCGImagePropertyExifFNumber as String])
let iso = positiveNumber(exif[kCGImagePropertyExifISOSpeedRatings as String])
let photographicEvidenceCount = [exposureTime, fNumber, iso].compactMap { $0 }.count
let provenance = !make.isEmpty && !model.isEmpty && photographicEvidenceCount >= (recognisedRaw ? 1 : 2)
result.merge(captureMetadata(source, tiff: tiff, exif: exif,
                             cameraProvenance: provenance, recognisedRaw: recognisedRaw)) { _, new in new }
var camera: [String: Any] = ["make": make, "model": model, "provenance": provenance,
    "recognised_raw": recognisedRaw, "photographic_evidence_count": photographicEvidenceCount]
if let v = exposureTime { camera["exposure_time"] = v }
if let v = fNumber { camera["f_number"] = v }
if let v = iso { camera["iso"] = v }
result["camera"] = camera
let originalWidth = (properties[kCGImagePropertyPixelWidth as String] as? NSNumber)?.intValue ?? 0
let originalHeight = (properties[kCGImagePropertyPixelHeight as String] as? NSNumber)?.intValue ?? 0
let orientationValue = (properties[kCGImagePropertyOrientation as String] as? NSNumber)?.intValue ?? 1
let swapsDimensions = [5, 6, 7, 8].contains(orientationValue)
var dimensions: [String: Int] = ["width": swapsDimensions ? originalHeight : originalWidth,
    "height": swapsDimensions ? originalWidth : originalHeight,
    "thumbnail_width": 0, "thumbnail_height": 0]
result["dimensions"] = dimensions
if !flags.isEmpty {
    result["reason"] = "screenshot_marker"
    emit(result)
}
if !provenance {
    result["reason"] = "missing_camera_provenance"
    emit(result)
}
let options: [CFString: Any] = [
    kCGImageSourceCreateThumbnailFromImageAlways: true,
    kCGImageSourceCreateThumbnailWithTransform: true,
    kCGImageSourceThumbnailMaxPixelSize: 1200,
    kCGImageSourceShouldCacheImmediately: true
]
guard let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
    result["reason"] = "thumbnail_decode_failed"
    emit(result)
}
dimensions["thumbnail_width"] = thumbnail.width
dimensions["thumbnail_height"] = thumbnail.height
result["dimensions"] = dimensions
let request = VNDetectFaceRectanglesRequest()
request.usesCPUOnly = true
result["detector_version"] = "\(implementationVersion)/VNDetectFaceRectanglesRequest-rev\(request.revision)"
let handler = VNImageRequestHandler(cgImage: thumbnail, orientation: .up, options: [:])
do {
    try handler.perform([request])
    let detected = request.results ?? []
    result["detected_face_count"] = detected.count
    let acceptedFaces = detected.filter {
        $0.confidence >= minimumConfidence &&
        $0.boundingBox.width * CGFloat(thumbnail.width) >= minimumFacePixels &&
        $0.boundingBox.height * CGFloat(thumbnail.height) >= minimumFacePixels
    }.sorted { $0.boundingBox.width * $0.boundingBox.height > $1.boundingBox.width * $1.boundingBox.height }
    let faces: [[String: Any]] = acceptedFaces.map { observation in
        let box = observation.boundingBox
        return ["x": Double(box.minX), "y": Double(1 - box.maxY),
                "width": Double(box.width), "height": Double(box.height),
                "confidence": Double(observation.confidence)]
    }
    result["faces"] = faces
    result["face_count"] = faces.count
    result["accepted"] = !faces.isEmpty
    result["reason"] = faces.isEmpty ? "no_confident_human_face" : "accepted_camera_portrait"
    emit(result)
} catch {
    result["reason"] = "vision_failed"
    result["error"] = String(describing: error)
    emit(result)
}
