# Bing Image Picker

An Anki add-on that adds a **🖼** button to the note editor. Press it to search
**Bing Images** for the note's word and pick from a 3×3 grid of thumbnails — the
chosen image is downloaded into your collection and written to a field.

<p align="center">
  <img src="docs/screenshot.png" alt="Screenshot" width="400">
</p>

The first time you press 🖼 on a note type, a dialog asks which field to search
from, which field to put the image in, and whether to append or overwrite.
Mappings are remembered and editable via **Tools → Bing Image Picker**.

## Install

In Anki, go to *Tools → Add-ons → Get Add-ons* and paste the code:

```
2112316511
```

To run from source, symlink this folder into Anki's add-on directory and restart:

```sh
ln -s "$PWD" ~/.local/share/Anki2/addons21/bing_image_picker
```
