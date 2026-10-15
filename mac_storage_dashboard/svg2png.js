// svg2png.js — renders an SVG to a square PNG with transparency, using the
// macOS app-icon grid (~10% margin) so artwork lines up with native icons in
// the Dock. Used by build_app.command to turn favicon.svg into the app icon.
//
// usage: osascript -l JavaScript svg2png.js <input.svg> <output.png> [size]
//
// Notes: NSImage can load SVG on macOS 11+. The output may be 2x the requested
// size on Retina displays (lockFocus follows the backing scale) — callers
// should not assume the exact pixel size.
ObjC.import('AppKit');
ObjC.import('Foundation');

function run(argv) {
  if (argv.length < 2) {
    throw new Error('usage: osascript -l JavaScript svg2png.js <input.svg> <output.png> [size]');
  }
  const src = argv[0], out = argv[1], size = Number(argv[2] || 1024);

  const img = $.NSImage.alloc.initWithContentsOfFile(src);
  if (!img.js) throw new Error('could not load ' + src);

  const canvas = $.NSImage.alloc.initWithSize($.NSMakeSize(size, size));
  canvas.lockFocus;
  const margin = Math.round(size * 0.0975);
  img.drawInRectFromRectOperationFraction(
    $.NSMakeRect(margin, margin, size - 2 * margin, size - 2 * margin),
    $.NSZeroRect, $.NSCompositingOperationSourceOver, 1.0);
  canvas.unlockFocus;

  const rep = $.NSBitmapImageRep.imageRepWithData(canvas.TIFFRepresentation);
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
  if (!png.writeToFileAtomically(out, true)) throw new Error('could not write ' + out);
  console.log('rendered ' + out + ' (' + rep.pixelsWide + 'x' + rep.pixelsHigh + ' px)');
}
