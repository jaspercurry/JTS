export function createSoundProfileApi() {
  async function jsonFromResponse(resp, fallbackMessage) {
    var payload;
    try {
      payload = await resp.json();
    } catch (e) {
      if (!resp.ok) throw new Error(fallbackMessage);
      throw e;
    }
    if (!resp.ok) throw new Error(fallbackMessage);
    return payload;
  }
  async function get(path, fallbackMessage) {
    return jsonFromResponse(await fetch(path, {cache: 'no-store'}), fallbackMessage);
  }
  return {
    get: get,
    jsonFromResponse: jsonFromResponse
  };
}
