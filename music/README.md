# Local music library

Drop audio files in this folder and the **My Library** station plays them:

```
music/
  Artist - Track Title.mp3
  mixes/late-night.flac
```

* Scanned recursively (up to 6 levels), following symlinks.
* Recognised extensions: `.mp3 .m4a .aac .flac .ogg .oga .opus .wav .wma .webm .mka .aiff .aif`
* `Artist - Title` filenames are split for the now-playing card; anything else
  is shown as-is.
* The folder is shuffled and recently played files are skipped, so a small
  library does not repeat itself.
* Nothing here is committed to git (see `.gitignore`).

Play it with `/lofi play station:library`, or point a custom station at another
folder with `/lofi station add name:"Room Tapes" url:"library:/srv/tapes"`.
