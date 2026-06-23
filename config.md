## Bing Image Picker

Field mappings are configured per note type from the GUI — press **🖼** in the
editor (first use prompts setup) or open **Tools → Bing Image Picker**. The same
dialog has a **SafeSearch** checkbox. You don't need to edit this JSON by hand,
but the format is:

```json
{
  "safe_search": null,      // true = filter explicit results; null until you pick
  "notetypes": {
    "My Note Type": {
      "source": "Word",     // field whose text is searched on Bing
      "target": "Image",    // field the chosen <img> is written to
      "mode": "append"      // "append" or "overwrite"
    }
  }
}
```
