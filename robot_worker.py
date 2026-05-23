import time
from core.database import list_rows, update_row, get_row, insert_error, insert_history
from core import statuses as st
from robot.downloader import download

while True:
    jobs = list_rows('autodoc_robot_queue', limit=5, filters={'status': 'PENDING'}, order_by='created_at', desc=False)
    if not jobs:
        time.sleep(5); continue
    for job in jobs:
        try:
            update_row('autodoc_robot_queue', job['id'], {'status':'RUNNING'})
            f = get_row('autodoc_files', job['file_id'])
            result = download(f.get('project_detected',''), f.get('autodoc_path',''), f.get('file_name'))
            update_row('autodoc_files', f['id'], {'local_path': result['local_path'], 'status': st.FILE_BAIXADO})
            update_row('autodoc_robot_queue', job['id'], {'status':'DONE','result':result})
            insert_history(email_id=f.get('email_id'), file_id=f['id'], action=st.ACTION_DOWNLOAD_REALIZADO, file_name=f.get('file_name'), to_path=result['local_path'], environment='HML', status='OK')
        except Exception as e:
            update_row('autodoc_robot_queue', job['id'], {'status':'ERROR','error_message':str(e)})
            insert_error('AUTODOC_DOWNLOAD_ERROR', str(e), payload=job, file_id=job.get('file_id'))
