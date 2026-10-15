# Homebrew cask template for the Mac Storage Dashboard app.
#
# To publish:
#   1. Run ./release.command — it builds dist/Mac-Storage-Dashboard.zip and
#      prints its sha256.
#   2. Create a GitHub release on Irfanwani/mac-storage-dashboard with tag
#      v<version> and upload the zip as an asset.
#   3. Copy this file into your tap repo as Casks/mac-storage-dashboard.rb
#      with `version` and `sha256` filled in.
#
# Users then install with:
#   brew install --cask irfanwani/tap/mac-storage-dashboard
cask "mac-storage-dashboard" do
  version "1.0.0"
  sha256 "REPLACE_WITH_SHA256_FROM_release.command"

  url "https://github.com/Irfanwani/mac-storage-dashboard/releases/download/v#{version}/Mac-Storage-Dashboard.zip"
  name "Mac Storage Dashboard"
  desc "Local, privacy-friendly disk dashboard: treemap, junk detection, safe cleanup"
  homepage "https://github.com/Irfanwani/mac-storage-dashboard"

  app "Mac Storage Dashboard.app"

  zap trash: [
    "~/Library/Application Support/Mac Storage Dashboard",
  ]
end
