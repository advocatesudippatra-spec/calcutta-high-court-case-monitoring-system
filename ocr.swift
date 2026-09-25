// Reads the text in an image (photo or scan of a notice) using macOS's built-in
// Vision text recognition. Usage: ocr <image file>   (prints the text, one line per row)
// Compiled by install.sh to ~/.calllist/bin/ocr.
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("usage: ocr <image file>\n".data(using: .utf8)!)
    exit(1)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = ["en-US"]
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
let lines = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))
