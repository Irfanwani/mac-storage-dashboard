# Homebrew cask for the Mac Storage Dashboard app.
#
# NOTE: this file is a TEMPLATE. The release workflow
# (.github/workflows/release.yml) fills in version + sha256 and copies it to
# Casks/mac-storage-dashboard.rb in the tap repo on every release — edit the
# template here, not the generated file in the tap.
cask "mac-storage-dashboard" do
  version "REPLACE_WITH_VERSION"
  sha256 "REPLACE_WITH_SHA256_FROM_release.command"

  url "https://github.com/Irfanwani/mac-storage-dashboard/releases/download/v#{version}/Mac-Storage-Dashboard.zip"
  name "Mac Storage Dashboard"
  desc "Local, privacy-friendly disk dashboard: treemap, junk detection, safe cleanup"
  homepage "https://github.com/Irfanwani/mac-storage-dashboard"

  # the app is a small native launcher around the bundled Python server
  depends_on formula: "python"

  app "Mac Storage Dashboard.app"

  zap trash: [
    "~/Library/Application Support/Mac Storage Dashboard",
  ]
end
