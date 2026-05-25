from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import settings
from services.graph_auth import GRAPH_BASE_URL, graph_health, graph_json, graph_request, get_graph_token
from services.normalizer import safe_path_part


class SharePointService:
    """Serviço SharePoint usando Microsoft Graph com token renovável.

    Compatível com a interface antiga:
    - token()
    - get/post/put_bytes/patch
    - site_by_url/drives/find_drive/children/item_by_path/ensure_folder/upload_small/move_item
    """

    def token(self) -> str:
        return get_graph_token(force_refresh=False)

    def health(self) -> Dict[str, Any]:
        base = graph_health()
        base.update(
            {
                "mode": "multi-site",
                "hostname": settings.sharepoint_hostname,
                "default_library": settings.sharepoint_default_library_name,
                "hml_root": settings.sharepoint_hml_root,
            }
        )
        return base

    def _full_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return GRAPH_BASE_URL + url
        return GRAPH_BASE_URL + "/" + url

    def get(self, url: str) -> Dict[str, Any]:
        return graph_json("GET", self._full_url(url))

    def post(self, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return graph_json(
            "POST",
            self._full_url(url),
            json=body or {},
            headers={"Content-Type": "application/json"},
        )

    def put_bytes(self, url: str, data: bytes) -> Dict[str, Any]:
        response = graph_request(
            "PUT",
            self._full_url(url),
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=300,
            retry_on_401=True,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Graph PUT {response.status_code}: {response.text}")
        return response.json() if response.text else {}

    def patch(self, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return graph_json(
            "PATCH",
            self._full_url(url),
            json=body or {},
            headers={"Content-Type": "application/json"},
        )

    def site_by_url(self, hostname: str, site_path: str) -> Dict[str, Any]:
        site_path = site_path if site_path.startswith("/") else "/" + site_path
        return self.get(f"/sites/{hostname}:{site_path}")

    def drives(self, site_id: str) -> List[Dict[str, Any]]:
        return self.get(f"/sites/{site_id}/drives").get("value", [])

    def find_drive(self, site_id: str, library_name: Optional[str] = None) -> Dict[str, Any]:
        library_name = library_name or settings.sharepoint_default_library_name
        drives = self.drives(site_id)
        for drive in drives:
            if (drive.get("name") or "").lower() == library_name.lower():
                return drive
        if drives:
            return drives[0]
        raise RuntimeError(f"Nenhum drive encontrado para site {site_id}")

    def children(self, drive_id: str, folder_path: str = "") -> List[Dict[str, Any]]:
        path = folder_path.strip("/ ")
        if path:
            url = f"/drives/{drive_id}/root:/{path}:/children"
        else:
            url = f"/drives/{drive_id}/root/children"
        return self.get(url).get("value", [])

    def item_by_path(self, drive_id: str, path: str) -> Optional[Dict[str, Any]]:
        path = path.strip("/ ")
        try:
            return self.get(f"/drives/{drive_id}/root:/{path}")
        except RuntimeError as exc:
            if "404" in str(exc) or "itemNotFound" in str(exc):
                return None
            raise

    def ensure_folder(self, drive_id: str, folder_path: str) -> Dict[str, Any]:
        current = ""
        last: Optional[Dict[str, Any]] = None
        parts = [safe_path_part(part) for part in folder_path.strip("/").split("/") if part.strip()]

        for part in parts:
            parent = current
            current = f"{current}/{part}".strip("/")
            existing = self.item_by_path(drive_id, current)
            if existing:
                last = existing
                continue

            body = {"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
            if parent:
                last = self.post(f"/drives/{drive_id}/root:/{parent}:/children", body)
            else:
                last = self.post(f"/drives/{drive_id}/root/children", body)

        return last or self.get(f"/drives/{drive_id}/root")

    def upload_small(self, drive_id: str, folder_path: str, file_name: str, local_path: str) -> Dict[str, Any]:
        folder_path = folder_path.strip("/ ")
        self.ensure_folder(drive_id, folder_path)
        safe_name = safe_path_part(file_name)
        full_path = f"{folder_path}/{safe_name}".strip("/")
        data = Path(local_path).read_bytes()
        return self.put_bytes(f"/drives/{drive_id}/root:/{full_path}:/content", data)

    def move_item(self, drive_id: str, source_path: str, target_folder_path: str, new_name: str) -> Dict[str, Any]:
        item = self.item_by_path(drive_id, source_path)
        if not item:
            raise RuntimeError(f"Arquivo origem não encontrado: {source_path}")
        parent = self.ensure_folder(drive_id, target_folder_path)
        body = {"parentReference": {"id": parent["id"]}, "name": safe_path_part(new_name)}
        return self.patch(f"/drives/{drive_id}/items/{item['id']}", body)


sharepoint = SharePointService()
