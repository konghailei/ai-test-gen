/**
 * 产品选择上下文 — 全局管理当前选中的产品
 */
import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import { products as prodApi } from '../api/client'
import type { Product } from '../types'

const STORAGE_KEY = 'aitestgen_selected_product'
const NAME_KEY = 'aitestgen_selected_product_name'

interface ProductContextType {
  products: Product[]
  selectedProductId: string
  selectedProduct: Product | null
  selectedProductName: string
  setSelectedProductId: (id: string) => void
  setSelectedProduct: (id: string, name: string) => void
  refreshProducts: () => Promise<void>
  loading: boolean
}

const ProductContext = createContext<ProductContextType>({
  products: [],
  selectedProductId: '',
  selectedProduct: null,
  selectedProductName: '',
  setSelectedProductId: () => {},
  setSelectedProduct: () => {},
  refreshProducts: async () => {},
  loading: false,
})

export function ProductProvider({ children }: { children: ReactNode }) {
  const [products, setProducts] = useState<Product[]>([])
  const [selectedProductId, setSelectedProductIdState] = useState<string>(() => localStorage.getItem(STORAGE_KEY) || '')
  const [selectedProductName, setSelectedProductName] = useState<string>(() => localStorage.getItem(NAME_KEY) || '')
  const [loading, setLoading] = useState(false)

  const setSelectedProduct = useCallback((id: string, name: string) => {
    setSelectedProductIdState(id)
    setSelectedProductName(name)
    localStorage.setItem(STORAGE_KEY, id)
    localStorage.setItem(NAME_KEY, name)
  }, [])

  const setSelectedProductId = useCallback((id: string) => {
    setSelectedProductIdState(id)
    localStorage.setItem(STORAGE_KEY, id)
  }, [])

  const refreshProducts = useCallback(async () => {
    setLoading(true)
    try {
      const list = await prodApi.list()
      setProducts(list || [])
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  const selectedProduct = products.find(p => p.id === selectedProductId) || null

  return (
    <ProductContext.Provider value={{
      products, selectedProductId, selectedProduct, selectedProductName,
      setSelectedProductId, setSelectedProduct, refreshProducts, loading,
    }}>
      {children}
    </ProductContext.Provider>
  )
}

export function useProduct() {
  return useContext(ProductContext)
}
