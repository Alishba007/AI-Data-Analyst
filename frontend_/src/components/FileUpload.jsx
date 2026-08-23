import { useRef, useState } from "react";
import { uploadFile } from "../api";

export default function FileUpload({ onUploaded }) {
  const inputRef = useRef(null);
  const [status, setStatus] = useState("idle"); // idle | uploading | done | error
  const [message, setMessage] = useState("");

  async function handleFile(file) {
    if (!file) return;
    setStatus("uploading");
    setMessage(`Reading ${file.name}…`);
    try {
      const result = await uploadFile(file);
      setStatus("done");
      setMessage(`${result.table_name} — ${result.row_count} rows loaded`);
      onUploaded?.(result);
    } catch (err) {
      setStatus("error");
      const detail = err?.response?.data?.detail || "Upload failed. Check the file format.";
      setMessage(detail);
    }
  }

  return (
    <div className="file-upload">
      <input
        ref={inputRef}
        type="file"
        accept=".csv,.xlsx,.xls"
        onChange={(e) => handleFile(e.target.files?.[0])}
        style={{ display: "none" }}
      />
      <button
        className="file-upload__button"
        onClick={() => inputRef.current?.click()}
        disabled={status === "uploading"}
      >
        {status === "uploading" ? "Uploading…" : "Upload data"}
      </button>
      {message && (
        <span className={`file-upload__status file-upload__status--${status}`}>{message}</span>
      )}
    </div>
  );
}
