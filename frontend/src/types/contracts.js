/** @typedef {{id:number, title:string, payload:object, author:string}} BusinessCase */
/** @typedef {{id:number, case_id:number, status:'queued'|'running'|'complete'|'failed'}} Run */
/** @typedef {{id:number, run_id:number, digest:string, input_fingerprint?:string,
  reviewer?:string, review_note?:string, reviewed_at?:string, staled_at?:string|null,
  stale_reason?:string|null, report:{summary:string, input_fingerprint?:string,
  metrics:object, sections:Array}}} Evidence */
