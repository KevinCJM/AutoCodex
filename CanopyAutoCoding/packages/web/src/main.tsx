import { render } from 'solid-js/web'
import { App } from './App'
import './styles/app.css'

const dispose = render(() => <App />, document.getElementById('root')!)

if (import.meta.hot) {
  import.meta.hot.dispose(dispose)
}
